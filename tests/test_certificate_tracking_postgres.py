"""Opt-in integration checks against a disposable, local Unix-socket PostgreSQL.

Never uses the application's configured database. Each test owns a random schema.
Set CERTIFICATE_TRACKING_TEST_SOCKET and CERTIFICATE_TRACKING_TEST_PORT explicitly.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.chatwoot.certificates import CertificateInventory, HistoricalCertificateIssuance
from scripts.backfill_certificate_history import revalidate, verify_saved


@pytest.fixture
async def inventory():
    socket = os.environ.get("CERTIFICATE_TRACKING_TEST_SOCKET")
    if not socket:
        pytest.skip("explicit disposable local PostgreSQL socket required")
    if not str(Path(socket).resolve()).startswith("/private/tmp/nevidimy-certificate-pg."):
        pytest.fail("only the task's disposable Unix-socket PostgreSQL is allowed")
    url = URL.create(
        "postgresql+asyncpg", database="postgres",
        query={"host": socket, "port": os.environ["CERTIFICATE_TRACKING_TEST_PORT"]},
    )
    admin = create_async_engine(url)
    schema = "certificate_test_" + uuid.uuid4().hex
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    instance = CertificateInventory(str(url), identity_key="synthetic-test-key", account_id=1)
    await instance._engine.dispose()
    instance._engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        yield instance
    finally:
        await instance.close()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def history(contact=7, message=41):
    return HistoricalCertificateIssuance(
        account_id=1, contact_id=contact, conversation_id=9,
        source_message_id=message, accepted_at=datetime(2026, 10, 20, 11, tzinfo=UTC),
    )


async def document(inventory, number=1):
    async with inventory._engine.begin() as connection:
        return (await connection.execute(text("""
            INSERT INTO women_help_pdf_certificates
                (pool_id, nominal_rubles, activation_code, serial_number, expires_at,
                 pdf_bucket, pdf_object_key, pdf_sha256, pdf_size, pdf_filename)
            SELECT id, 3000, :code, :serial, now() + interval '30 days',
                'test-only', :object, :sha, 42, 'synthetic.pdf'
            FROM women_help_certificate_pools WHERE slug = 'pyaterochka'
            RETURNING id
        """), {"code": f"INVALID-{number}", "serial": f"INVALID-SERIAL-{number}",
               "object": f"synthetic/{number}", "sha": f"{number:064x}"})).scalar_one()


async def scalar(inventory, query):
    async with inventory._engine.connect() as connection:
        return (await connection.execute(text(query))).scalar_one()


async def test_migration_of_existing_document_table_preserves_real_pdf(inventory):
    await inventory.initialize()
    await document(inventory)
    # Reconstitute the previous table shape, then exercise the actual migration.
    async with inventory._engine.begin() as connection:
        await connection.execute(text("DROP TABLE women_help_certificate_requests"))
        await connection.execute(text("""
            ALTER TABLE women_help_pdf_certificates
                DROP CONSTRAINT women_help_pdf_certificates_record_check,
                DROP COLUMN record_kind, DROP COLUMN account_id, DROP COLUMN contact_id,
                DROP COLUMN conversation_id, DROP COLUMN source_message_id,
                DROP COLUMN accepted_at,
                ALTER COLUMN pool_id SET NOT NULL,
                ALTER COLUMN nominal_rubles SET NOT NULL,
                ALTER COLUMN activation_code SET NOT NULL,
                ALTER COLUMN serial_number SET NOT NULL,
                ALTER COLUMN expires_at SET NOT NULL,
                ALTER COLUMN pdf_bucket SET NOT NULL,
                ALTER COLUMN pdf_object_key SET NOT NULL,
                ALTER COLUMN pdf_sha256 SET NOT NULL,
                ALTER COLUMN pdf_size SET NOT NULL,
                ALTER COLUMN pdf_filename SET NOT NULL,
                ADD CONSTRAINT women_help_pdf_certificates_status_check
                    CHECK (status IN ('available','reserved','submitted','delivered','failed'))
        """))
        await connection.execute(text("DROP TABLE women_help_certificate_schema_migrations"))
    await inventory.initialize()
    await inventory.initialize()
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 1
    assert sum(pool.total for pool in await inventory.stock()) == 1
    assert (await inventory.claim("food_card", "fresh", 8)).status == "issued"


async def test_seventeen_history_rows_are_atomic_repeatable_and_not_stock(inventory):
    await inventory.initialize()
    rows = [history(contact=100 + index, message=200 + index) for index in range(17)]
    assert await inventory.import_historical_issuances(rows) == (17, 0)
    assert await inventory.import_historical_issuances(rows) == (0, 17)
    assert sum(pool.total for pool in await inventory.stock()) == 0
    for row in rows:
        claim = await inventory.claim("food_card", "unused", row.contact_id)
        assert claim.status == "already_issued" and claim.certificate is None
        # The pause's exact-only recovery semantics intentionally stay unchanged.
        assert (await inventory.resume("food_card", "unused", row.contact_id)).status == "unavailable"
    assert await scalar(inventory, """
        SELECT count(*) FROM women_help_pdf_certificates
        WHERE record_kind = 'historical_issuance' AND pool_id IS NULL
          AND activation_code IS NULL AND pdf_object_key IS NULL AND delivered_at IS NULL
    """) == 17


async def test_conflicting_history_rolls_back_entire_batch(inventory):
    await inventory.initialize()
    await inventory.import_historical_issuances([history()])
    with pytest.raises(ValueError):
        await inventory.import_historical_issuances([history(8, 42), history(7, 99)])
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 1


async def test_history_reconciles_requests_created_before_backfill(inventory):
    await inventory.initialize()
    assert await inventory.record_request("food_card", "before-pyaterochka", 7, 9, 51, "paused")
    assert await inventory.record_request("medicine_card", "before-ozon", 7, 9, 52, "paused")
    assert await inventory.import_historical_issuances([history()]) == (1, 0)
    assert await scalar(inventory, """
        SELECT count(*) FROM women_help_certificate_requests
        WHERE status = 'fulfilled' AND fulfilled_certificate_id IS NOT NULL
    """) == 2
    assert not await inventory.record_request("food_card", "after-backfill", 7, 9, 53, "paused")


async def test_pdf_required_fields_remain_enforced(inventory):
    await inventory.initialize()
    with pytest.raises(IntegrityError):
        async with inventory._engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO women_help_pdf_certificates (status) VALUES ('available')
            """))
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 0


async def test_waiting_replays_preserve_first_order_and_history_is_excluded(inventory):
    await inventory.initialize()
    await inventory.import_historical_issuances([history()])
    assert not await inventory.record_request("food_card", "issued", 7, 9, 50, "paused")
    results = await asyncio.gather(*[
        inventory.record_request("food_card", "same-event", 8, 9, 51, "paused")
        for _ in range(4)
    ])
    assert results.count(True) == 1
    first = await scalar(inventory, "SELECT first_requested_at FROM women_help_certificate_requests")
    assert await inventory.record_request("children_card", "new-event", 8, 9, 52, "paused")
    assert not await inventory.record_request("food_card", "same-event", 8, 9, 51, "paused")
    assert await scalar(inventory, "SELECT first_requested_at FROM women_help_certificate_requests") == first
    assert await scalar(inventory, "SELECT count(*) FROM women_help_certificate_requests") == 1
    assert await inventory.record_request("medicine_card", "other-pool", 8, 9, 53, "paused")
    assert await scalar(inventory, "SELECT count(*) FROM women_help_certificate_requests") == 2
    async with inventory._engine.begin() as connection:
        await connection.execute(text("UPDATE women_help_certificate_requests SET status = 'cancelled'"))
    assert not await inventory.record_request("food_card", "same-event", 8, 9, 51, "paused")
    assert await scalar(inventory, """
        SELECT count(*) FROM women_help_certificate_requests WHERE status = 'waiting'
    """) == 0


async def test_historical_import_and_claim_share_recipient_lock(inventory):
    await inventory.initialize()
    await document(inventory)
    results = await asyncio.gather(
        inventory.import_historical_issuances([history()]),
        inventory.claim("food_card", "parallel-claim", 7),
        return_exceptions=True,
    )
    assert not any(isinstance(value, BaseException) and not isinstance(value, ValueError)
                   for value in results)
    assert await scalar(inventory, """
        SELECT count(*) FROM women_help_pdf_certificates WHERE recipient_hash IS NOT NULL
    """) == 1
    claim = results[1]
    assert claim.status in {"already_issued", "issued"}
    if claim.status == "issued":
        assert isinstance(results[0], ValueError)
    else:
        assert results[0] == (1, 0)


async def source_message(inventory):
    """Minimal synthetic Chatwoot metadata for exercising the actual audit SQL."""
    async with inventory._engine.begin() as connection:
        for statement in (
            "CREATE TABLE channel_telegram (id bigint, bot_name text)",
            "CREATE TABLE inboxes (id bigint, account_id bigint, channel_id bigint, channel_type text)",
            ("CREATE TABLE conversations (id bigint, contact_id bigint, display_id bigint, "
             "account_id bigint, inbox_id bigint)"),
            ("CREATE TABLE messages (id bigint, conversation_id bigint, created_at timestamp, "
             "status integer, source_id text, content text, account_id bigint, inbox_id bigint, "
             "message_type integer, private boolean)"),
            "CREATE TABLE attachments (id bigint, message_id bigint)",
            ("CREATE TABLE active_storage_attachments (record_type text, record_id bigint, "
             "name text, blob_id bigint)"),
            "CREATE TABLE active_storage_blobs (id bigint, content_type text, filename text)",
            "INSERT INTO channel_telegram VALUES (1, 'synthetic_bot')",
            "INSERT INTO inboxes VALUES (5, 1, 1, 'Channel::Telegram')",
            "INSERT INTO conversations VALUES (90, 7, 9, 1, 5)",
            "INSERT INTO attachments VALUES (1, 41)",
            "INSERT INTO active_storage_attachments VALUES ('Attachment', 1, 'file', 1)",
            ("INSERT INTO active_storage_blobs VALUES "
             "(1, 'application/pdf', 'synthetic-ozon-certificate.pdf')"),
        ):
            await connection.execute(text(statement))
        await connection.execute(text("""
            INSERT INTO messages VALUES (
                41, 90, '2026-10-20 11:00:00', 0, 'synthetic-accepted',
                :content, 1, 5, 1, false
            )
        """), {"content": "Тестовый сертификат"})
    return {"scope": {"id": 5, "bot_name": "synthetic_bot"}}, [{
        "message_id": 41, "contact_id": 7, "conversation_id": 9,
        "created_at": "2026-10-20 11:00:00",
        "first_request_at": datetime(2026, 10, 20, 10, tzinfo=UTC),
    }]


async def test_backfill_revalidates_source_and_reads_committed_evidence(inventory):
    await inventory.initialize()
    report, selected = await source_message(inventory)
    rows = await revalidate(inventory, report, selected)
    assert len(rows) == 1 and rows[0].provider_slug == "ozon"
    assert await inventory.import_historical_issuances(rows) == (1, 0)
    await verify_saved(inventory, rows)


@pytest.mark.parametrize("change", [
    "UPDATE messages SET source_id = NULL",
    "UPDATE messages SET status = 3",
    "UPDATE messages SET private = true",
    "UPDATE conversations SET contact_id = 8",
    "UPDATE active_storage_blobs SET content_type = 'image/png'",
    "UPDATE channel_telegram SET bot_name = 'different_synthetic_bot'",
])
async def test_backfill_refuses_changed_source(inventory, change):
    await inventory.initialize()
    report, selected = await source_message(inventory)
    async with inventory._engine.begin() as connection:
        await connection.execute(text(change))
    with pytest.raises(ValueError):
        await revalidate(inventory, report, selected)
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 0

"""Opt-in, disposable local PostgreSQL checks; never uses configured production DB."""

import os
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.chatwoot.certificates import CertificateInventory
from app.chatwoot.manual_certificate_history import (
    ManualHistoricalIssuance,
    import_manual_history,
    initialize_manual_schema,
    verify_manual_history,
)


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
    schema = "manual_history_test_" + uuid.uuid4().hex
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    instance = CertificateInventory(str(url), identity_key="synthetic-test-key", account_id=1)
    await instance._engine.dispose()
    instance._engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        await instance.initialize()
        async with instance._engine.begin() as connection:
            await connection.execute(text("DROP TABLE women_help_certificate_requests"))
        yield instance
    finally:
        await instance.close()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def record(index=0, *, accepted=True):
    return ManualHistoricalIssuance(
        account_id=1, contact_id=100 + index, conversation_id=200 + index,
        source_message_id=300 + index, issued_on=date(2026, 10, 1),
        accepted_at=datetime(2026, 10, 3, 12, tzinfo=UTC) if accepted else None,
        provider_slug="ozon", aid_id="children_card",
        source_reference=f"synthetic-sheet:gid=0:row={index + 2}",
        evidence={"kind": "user_attested_sheet", "row": index + 2},
    )


async def import_rows(connection, records):
    return await import_manual_history(
        connection, records, identity_key="synthetic-test-key", account_id=1,
    )


async def scalar(inventory, query):
    async with inventory._engine.connect() as connection:
        return (await connection.execute(text(query))).scalar_one()


async def test_thirty_nine_rows_repeat_readback_and_never_create_requests_or_stock(inventory):
    records = [record(index, accepted=index != 38) for index in range(39)]
    async with inventory._engine.begin() as connection:
        await initialize_manual_schema(connection)
        await initialize_manual_schema(connection)
        assert await import_rows(connection, records) == (39, 0)
    async with inventory._engine.begin() as connection:
        assert await import_rows(connection, records) == (0, 39)
        assert await verify_manual_history(
            connection, records, identity_key="synthetic-test-key", account_id=1,
        ) == 39
    assert await scalar(inventory, """
        SELECT count(*) FROM women_help_pdf_certificates
        WHERE status = 'accepted' AND accepted_at IS NOT NULL
          AND issued_on = '2026-10-01' AND pool_id IS NOT NULL
    """) == 38
    assert await scalar(inventory, """
        SELECT count(*) FROM women_help_pdf_certificates
        WHERE status = 'recorded' AND accepted_at IS NULL
          AND activation_code IS NULL AND pdf_object_key IS NULL
          AND issuance_key IS NULL AND NOT is_test
    """) == 1
    assert await scalar(inventory, "SELECT to_regclass('women_help_certificate_requests')") is None
    assert sum(pool.total for pool in await inventory.stock()) == 0
    assert (await inventory.claim("children_card", "synthetic-claim", 138)).status == "already_issued"


async def test_v2_upgrades_unversioned_table_preserving_real_document_and_strict_fields(inventory):
    async with inventory._engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO women_help_pdf_certificates
                (pool_id, nominal_rubles, activation_code, serial_number, expires_at,
                 pdf_bucket, pdf_object_key, pdf_sha256, pdf_size, pdf_filename)
            SELECT id, 3000, 'INVALID-SYNTHETIC-CODE', 'INVALID-SERIAL', now(),
                   'test-only', 'synthetic/object', repeat('a',64), 42, 'synthetic.pdf'
            FROM women_help_certificate_pools WHERE slug = 'ozon'
        """))
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
        await initialize_manual_schema(connection)
    assert await scalar(inventory, "SELECT count(*) FROM women_help_certificate_schema_migrations") == 2
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 1
    assert await scalar(inventory, "SELECT to_regclass('women_help_certificate_requests')") is None
    with pytest.raises(IntegrityError):
        async with inventory._engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO women_help_pdf_certificates (status) VALUES ('available')
            """))
    with pytest.raises(IntegrityError):
        async with inventory._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates SET issued_on = '2026-10-01'
            """))


async def test_v2_preserves_legacy_evidence_and_future_v1_initialization(inventory):
    async with inventory._engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO women_help_pdf_certificates
                (record_kind, status, recipient_hash, account_id, contact_id,
                 conversation_id, source_message_id, accepted_at)
            VALUES ('historical_issuance', 'accepted', 'synthetic-hash', 1, 7, 11, 13, now())
        """))
        await initialize_manual_schema(connection)
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 1
    # The ordinary v1 schema helper must not remove v2 columns or recorded rows.
    from app.chatwoot.certificate_schema import initialize_schema

    async with inventory._engine.begin() as connection:
        await initialize_schema(connection)
        assert await import_rows(connection, [record(accepted=False)]) == (1, 0)


async def test_entire_batch_rolls_back_on_conflict(inventory):
    async with inventory._engine.begin() as connection:
        await initialize_manual_schema(connection)
        await import_rows(connection, [record()])
    with pytest.raises(ValueError, match="conflicts"):
        async with inventory._engine.begin() as connection:
            await import_rows(connection, [record(1), record(accepted=False)])
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 1


async def test_missing_known_pool_is_an_error_and_import_leaves_no_rows(inventory):
    async with inventory._engine.begin() as connection:
        await initialize_manual_schema(connection)
        await connection.execute(text("DELETE FROM women_help_certificate_pools WHERE slug='ozon'"))
    with pytest.raises(ValueError, match="pool is missing"):
        async with inventory._engine.begin() as connection:
            await import_rows(connection, [record()])
    assert await scalar(inventory, "SELECT count(*) FROM women_help_pdf_certificates") == 0


@pytest.mark.parametrize("change", [
    "accepted_at = NULL", "status = 'recorded'", "issued_on = NULL",
    "source_reference = NULL", "historical_evidence = '[]'::jsonb",
    "source_message_id = 0", "pool_id = NULL",
    "activation_code = 'INVALID-UNEXPECTED'", "is_test = true",
])
async def test_v2_rejects_invalid_history_state(inventory, change):
    async with inventory._engine.begin() as connection:
        await initialize_manual_schema(connection)
        await import_rows(connection, [record()])
    with pytest.raises(IntegrityError):
        async with inventory._engine.begin() as connection:
            await connection.execute(text(f"UPDATE women_help_pdf_certificates SET {change}"))

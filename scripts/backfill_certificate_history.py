"""Revalidate an existing private audit and import only its accepted PDF recipients.

Dry-run is the default. This command never downloads PDFs, sends messages, or
initializes/migrates a database. Apply only after a verified backup and migration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.chatwoot.certificates import (
    CertificateInventory,
    HistoricalCertificateIssuance,
    database_url,
)
from app.config import settings


def timestamp(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    # Chatwoot's PostgreSQL created_at is timestamp without timezone, stored in UTC.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def candidates(report: dict, expected: int) -> list[dict]:
    if expected <= 0:
        raise ValueError("expected recipient count must be positive")
    selected = [
        row for row in report["rows"]
        if row["category"] == "explicit_waitlist"
        and row["delivery_audit"] == "manual_certificate_sent_to_telegram"
    ]
    if len(selected) != expected or len({row["contact_id"] for row in selected}) != expected:
        raise ValueError("audit recipient count does not match explicit expectation")
    result = []
    for row in selected:
        first_request = timestamp(row["first_confirmed_request"])
        evidence = [
            message for message in row["manual_pdf_messages"]
            if message["certificate_filename"] and message["body_mentions_certificate"]
            and message["external_receipt"] and message["status"] != 3
            and timestamp(message["created_at"]) >= first_request
            and message["contact_id"] == row["contact_id"]
        ]
        if not evidence:
            raise ValueError("audited recipient lacks accepted post-request PDF evidence")
        original = min(evidence, key=lambda message: (
            timestamp(message["created_at"]), message["message_id"],
        ))
        result.append({**original, "first_request_at": first_request})
    if len({item["message_id"] for item in result}) != expected:
        raise ValueError("source messages must identify distinct recipients")
    return sorted(result, key=lambda item: item["contact_id"])


async def revalidate(inventory: CertificateInventory, report: dict, selected: list[dict]):
    """Return technical evidence only; never load message text or bearer values."""
    account_id = inventory._account_id
    inbox_id = report["scope"]["id"]
    bot_name = report["scope"]["bot_name"]
    async with inventory._engine.connect() as connection, connection.begin():
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        await connection.execute(text("SET LOCAL statement_timeout = '15s'"))
        identity = (await connection.execute(text("""
                SELECT t.bot_name FROM inboxes i
                JOIN channel_telegram t ON t.id = i.channel_id
                WHERE i.id = :inbox_id AND i.account_id = :account_id
                  AND i.channel_type = 'Channel::Telegram'
            """), {"inbox_id": inbox_id, "account_id": account_id})).scalar_one()
        if identity != bot_name:
            raise ValueError("audit inbox identity no longer matches database")
        confirmed = []
        for item in selected:
            rows = (await connection.execute(text("""
                    SELECT m.id, c.contact_id, c.display_id conversation_id, m.created_at,
                        m.status, m.source_id IS NOT NULL AND m.source_id != '' accepted,
                        b.content_type = 'application/pdf' is_pdf,
                        b.filename ~* '(certificate|certificat|ozon|pyater|сертифик|озон|пят[её]р)'
                            certificate_filename,
                        m.content LIKE '%сертификат%' body_mentions_certificate,
                    CASE WHEN b.filename ~* '(ozon|озон)'
                              AND NOT b.filename ~* '(pyater|пят[её]р)' THEN 'ozon'
                         WHEN b.filename ~* '(pyater|пят[её]р)'
                              AND NOT b.filename ~* '(ozon|озон)' THEN 'pyaterochka'
                             ELSE NULL END provider_slug
                    FROM messages m JOIN conversations c ON c.id = m.conversation_id
                    JOIN attachments a ON a.message_id = m.id
                    JOIN active_storage_attachments aa
                      ON aa.record_type = 'Attachment' AND aa.record_id = a.id
                      AND aa.name = 'file'
                    JOIN active_storage_blobs b ON b.id = aa.blob_id
                    WHERE m.id = :message_id AND m.account_id = :account_id
                      AND c.account_id = :account_id AND m.inbox_id = :inbox_id
                      AND c.inbox_id = :inbox_id AND m.message_type = 1 AND NOT m.private
                """), {
                "message_id": item["message_id"], "account_id": account_id,
                "inbox_id": inbox_id,
            })).mappings().all()
            if len(rows) != 1:
                raise ValueError("source message is absent or has ambiguous attachments")
            row = rows[0]
            if not (
                row["contact_id"] == item["contact_id"]
                and row["conversation_id"] == item["conversation_id"]
                and row["status"] != 3 and row["accepted"] and row["is_pdf"]
                and row["certificate_filename"] and row["body_mentions_certificate"]
                and timestamp(row["created_at"]) == timestamp(item["created_at"])
                and timestamp(row["created_at"]) >= item["first_request_at"]
            ):
                raise ValueError("historical source no longer satisfies audit criteria")
            confirmed.append(HistoricalCertificateIssuance(
                account_id=account_id, contact_id=row["contact_id"],
                conversation_id=row["conversation_id"], source_message_id=row["id"],
                accepted_at=timestamp(row["created_at"]),
                provider_slug=row["provider_slug"],
            ))
        return confirmed


async def run(report_path: Path, *, expected: int, apply: bool) -> dict:
    metadata = report_path.stat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("audit must be an owner-only regular file")
    report = json.loads(report_path.read_text())
    selected = candidates(report, expected)
    if not settings.certificate_database_password or not settings.certificate_identity_key:
        raise ValueError("database credentials and identity key are required")
    inventory = CertificateInventory(
        database_url(settings.certificate_database_password),
        identity_key=settings.certificate_identity_key, account_id=settings.chatwoot_account_id,
    )
    try:
        confirmed = await revalidate(inventory, report, selected)
        result = {"validated_recipients": len(confirmed), "apply": apply}
        if apply:
            imported, duplicates = await inventory.import_historical_issuances(confirmed)
            result.update(imported=imported, duplicates=duplicates)
            await verify_saved(inventory, confirmed)
            result["verified_recipients"] = len(confirmed)
        return result
    finally:
        await inventory.close()


async def verify_saved(inventory: CertificateInventory, confirmed: list) -> None:
    """Read back committed identities and evidence without disclosing their values."""
    async with inventory._engine.connect() as connection:
        for record in confirmed:
            row = (await connection.execute(text("""
                SELECT c.*, p.slug provider_slug FROM women_help_pdf_certificates c
                LEFT JOIN women_help_certificate_pools p ON p.id = c.pool_id
                WHERE c.account_id = :account_id AND c.source_message_id = :message_id
                  AND c.record_kind = 'historical_issuance'
            """), {
                "account_id": record.account_id, "message_id": record.source_message_id,
            })).mappings().one()
            expected = {
                "status": "accepted", "contact_id": record.contact_id,
                "conversation_id": record.conversation_id, "accepted_at": record.accepted_at,
                "provider_slug": record.provider_slug, "selected_aid_id": record.aid_id,
                "recipient_hash": inventory._recipient_hash(record.contact_id),
                "activation_code": None, "pdf_object_key": None, "delivered_at": None,
            }
            if any(row[key] != value for key, value in expected.items()):
                raise ValueError("committed historical evidence did not pass readback")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--expected-recipients", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(
            args.report, expected=args.expected_recipients, apply=args.apply,
        ))
    except Exception as error:  # noqa: BLE001 -- redact driver parameters and bearer values
        # Database/driver exceptions can contain query parameters; never print them.
        raise SystemExit("history_backfill_failed=" + type(error).__name__) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()

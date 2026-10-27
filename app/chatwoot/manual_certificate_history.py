"""Import user-attested issuance history without creating requests or documents.

All database helpers use the caller's transaction. A spreadsheet date describes the
issuance attestation; only independent transport evidence can set ``accepted_at``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from app.chatwoot.certificate_schema import SCHEMA_LOCK_KEY, initialize_schema

MANUAL_HISTORY_SCHEMA_VERSION = 2
# Historical classification deliberately does not change current allocation pools.
HISTORICAL_PROVIDER_AIDS = {
    "ozon": frozenset({"medicine_card", "hostel_3_nights", "children_card"}),
    "pyaterochka": frozenset({"food_card", "children_card"}),
}


@dataclass(frozen=True, slots=True)
class ManualHistoricalIssuance:
    account_id: int
    contact_id: int
    conversation_id: int
    source_message_id: int
    issued_on: date
    accepted_at: datetime | None
    provider_slug: str
    aid_id: str | None
    source_reference: str
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        for value in (
            self.account_id, self.contact_id, self.conversation_id, self.source_message_id,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("manual historical identifiers must be positive integers")
        if type(self.issued_on) is not date:
            raise ValueError("manual historical issuance date must be a date")
        if self.accepted_at is not None and (
            not isinstance(self.accepted_at, datetime)
            or self.accepted_at.tzinfo is None or self.accepted_at.utcoffset() is None
        ):
            raise ValueError("actual acceptance timestamp must include a timezone")
        if (not isinstance(self.provider_slug, str)
                or self.provider_slug not in HISTORICAL_PROVIDER_AIDS):
            raise ValueError("unknown manual historical provider")
        if (self.aid_id is not None
                and (not isinstance(self.aid_id, str)
                     or self.aid_id not in HISTORICAL_PROVIDER_AIDS[self.provider_slug])):
            raise ValueError("manual historical provider does not match aid")
        if not isinstance(self.source_reference, str) or not self.source_reference.strip():
            raise ValueError("manual historical source reference is required")
        if not isinstance(self.evidence, dict) or not self.evidence:
            raise ValueError("manual historical evidence must be a nonempty JSON object")
        try:
            encoded = json.dumps(self.evidence, allow_nan=False)
            if json.loads(encoded) != self.evidence:
                raise ValueError("manual historical evidence must be JSON-native")
        except (TypeError, ValueError) as error:
            raise ValueError("manual historical evidence must be JSON-native") from error


async def initialize_manual_schema(connection) -> None:
    """Upgrade only the existing PDF/history tables, under the shared schema lock."""
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": SCHEMA_LOCK_KEY},
    )
    await initialize_schema(connection)
    await initialize_manual_schema_v2(connection)


async def initialize_manual_schema_v2(connection) -> None:
    """Apply v2 after v1, with the caller holding the shared schema lock."""
    applied = (await connection.execute(text("""
        SELECT 1 FROM women_help_certificate_schema_migrations WHERE version = :version
    """), {"version": MANUAL_HISTORY_SCHEMA_VERSION})).first()
    if applied is not None:
        return
    await connection.execute(text("""
        ALTER TABLE women_help_pdf_certificates
            ADD COLUMN issued_on DATE,
            ADD COLUMN source_reference TEXT,
            ADD COLUMN historical_evidence JSONB,
            DROP CONSTRAINT women_help_pdf_certificates_record_check
    """))
    await connection.execute(text("""
        ALTER TABLE women_help_pdf_certificates
            ADD CONSTRAINT women_help_pdf_certificates_record_check CHECK (
                (record_kind = 'certificate'
                 AND pool_id IS NOT NULL AND nominal_rubles IS NOT NULL
                 AND activation_code IS NOT NULL AND serial_number IS NOT NULL
                 AND expires_at IS NOT NULL AND pdf_bucket IS NOT NULL
                 AND pdf_object_key IS NOT NULL AND pdf_sha256 IS NOT NULL
                 AND pdf_size IS NOT NULL AND pdf_filename IS NOT NULL
                 AND status IN ('available','reserved','submitted','delivered','failed')
                 AND account_id IS NULL AND contact_id IS NULL
                 AND conversation_id IS NULL AND source_message_id IS NULL
                 AND accepted_at IS NULL AND issued_on IS NULL
                 AND source_reference IS NULL AND historical_evidence IS NULL)
                OR
                (record_kind = 'historical_issuance'
                 AND ((status = 'accepted' AND accepted_at IS NOT NULL)
                      OR (status = 'recorded' AND accepted_at IS NULL
                          AND issued_on IS NOT NULL))
                 AND ((issued_on IS NULL AND source_reference IS NULL
                       AND historical_evidence IS NULL)
                      OR (issued_on IS NOT NULL AND pool_id IS NOT NULL
                          AND source_reference IS NOT NULL
                          AND length(btrim(source_reference)) > 0
                          AND historical_evidence IS NOT NULL
                          AND jsonb_typeof(historical_evidence) = 'object'
                          AND historical_evidence <> '{}'::jsonb))
                 AND account_id IS NOT NULL AND account_id > 0
                 AND contact_id IS NOT NULL AND contact_id > 0
                 AND conversation_id IS NOT NULL AND conversation_id > 0
                 AND source_message_id IS NOT NULL AND source_message_id > 0
                 AND recipient_hash IS NOT NULL
                 AND issuance_key IS NULL AND nominal_rubles IS NULL
                 AND activation_code IS NULL AND serial_number IS NULL
                 AND valid_from IS NULL AND expires_at IS NULL
                 AND pdf_bucket IS NULL AND pdf_object_key IS NULL
                 AND pdf_version_id IS NULL AND pdf_sha256 IS NULL
                 AND pdf_size IS NULL AND pdf_filename IS NULL AND NOT is_test
                 AND chatwoot_message_id IS NULL AND reserved_at IS NULL
                 AND submitted_at IS NULL AND delivered_at IS NULL)
            )
    """))
    await connection.execute(text("""
        INSERT INTO women_help_certificate_schema_migrations (version) VALUES (:version)
    """), {"version": MANUAL_HISTORY_SCHEMA_VERSION})


def _records_and_key(records, identity_key, account_id):
    if type(account_id) is not int or account_id <= 0:
        raise ValueError("Chatwoot account must be a positive integer")
    key = identity_key.encode() if isinstance(identity_key, str) else identity_key
    if not isinstance(key, bytes) or not key:
        raise ValueError("recipient identity key is required")
    records = tuple(records)
    if any(not isinstance(row, ManualHistoricalIssuance) for row in records):
        raise ValueError("typed manual historical records are required")
    if any(row.account_id != account_id for row in records):
        raise ValueError("manual historical issuance belongs to another Chatwoot account")
    return records, key


def _values(record, key):
    # Frozen records still contain a mutable evidence dictionary; revalidate at use.
    record.__post_init__()
    return {
        "account_id": record.account_id, "contact_id": record.contact_id,
        "conversation_id": record.conversation_id,
        "source_message_id": record.source_message_id, "issued_on": record.issued_on,
        "accepted_at": record.accepted_at, "provider_slug": record.provider_slug,
        "selected_aid_id": record.aid_id, "source_reference": record.source_reference,
        "historical_evidence": json.loads(json.dumps(record.evidence, allow_nan=False)),
        "recipient_hash": hmac.new(
            key, f"{record.account_id}:{record.contact_id}".encode(), hashlib.sha256,
        ).hexdigest(),
        "status": "accepted" if record.accepted_at is not None else "recorded",
    }


# Explicit columns ensure neither readback nor conflict handling retrieves bearer codes.
_READBACK_SQL = """
    SELECT c.record_kind, c.status, c.recipient_hash, c.account_id, c.contact_id,
           c.conversation_id, c.source_message_id, c.issued_on, c.accepted_at,
           c.selected_aid_id, c.source_reference, c.historical_evidence,
           p.slug AS provider_slug,
           (c.issuance_key IS NULL AND c.nominal_rubles IS NULL
            AND c.activation_code IS NULL AND c.serial_number IS NULL
            AND c.valid_from IS NULL AND c.expires_at IS NULL
            AND c.pdf_bucket IS NULL AND c.pdf_object_key IS NULL
            AND c.pdf_version_id IS NULL AND c.pdf_sha256 IS NULL
            AND c.pdf_size IS NULL AND c.pdf_filename IS NULL AND NOT c.is_test
            AND c.chatwoot_message_id IS NULL AND c.reserved_at IS NULL
            AND c.submitted_at IS NULL AND c.delivered_at IS NULL) AS history_only
    FROM women_help_pdf_certificates c
    LEFT JOIN women_help_certificate_pools p ON p.id = c.pool_id
    WHERE c.recipient_hash = :recipient_hash
       OR (c.account_id = :account_id AND c.source_message_id = :source_message_id)
"""


def _exact(rows, values):
    return (len(rows) == 1 and rows[0]["record_kind"] == "historical_issuance"
            and rows[0]["history_only"] is True
            and all(rows[0][key] == value for key, value in values.items()))


async def import_manual_history(
    connection, records: Iterable[ManualHistoricalIssuance], *, identity_key: str | bytes,
    account_id: int,
) -> tuple[int, int]:
    """Insert exact, repeatable history in the caller's all-or-nothing transaction."""
    records, key = _records_and_key(records, identity_key, account_id)
    values = [_values(record, key) for record in records]
    lock_keys = {
        int.from_bytes(bytes.fromhex(value["recipient_hash"][:16]), "big", signed=True)
        for value in values
    }
    for lock_key in sorted(lock_keys):
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key},
        )
    imported = duplicates = 0
    for value in values:
        prior = (await connection.execute(
            text(_READBACK_SQL + " FOR UPDATE OF c"), value,
        )).mappings().all()
        if prior:
            if not _exact(prior, value):
                raise ValueError("manual historical issuance conflicts with existing inventory")
            duplicates += 1
            continue
        params = {**value, "historical_evidence": json.dumps(value["historical_evidence"])}
        result = await connection.execute(text("""
            INSERT INTO women_help_pdf_certificates
                (record_kind, status, pool_id, recipient_hash, account_id, contact_id,
                 conversation_id, source_message_id, issued_on, accepted_at,
                 selected_aid_id, source_reference, historical_evidence)
            SELECT 'historical_issuance', :status, p.id, :recipient_hash,
                   :account_id, :contact_id, :conversation_id, :source_message_id,
                   :issued_on, :accepted_at, :selected_aid_id, :source_reference,
                   CAST(:historical_evidence AS JSONB)
            FROM women_help_certificate_pools p WHERE p.slug = :provider_slug
            ON CONFLICT DO NOTHING RETURNING id
        """), params)
        if result.first() is None:
            raise ValueError("manual historical pool is missing or issuance conflicts")
        imported += 1
    return imported, duplicates


async def verify_manual_history(
    connection, records: Iterable[ManualHistoricalIssuance], *, identity_key: str | bytes,
    account_id: int,
) -> int:
    """Read back every record and its absent document state, without fetching codes."""
    records, key = _records_and_key(records, identity_key, account_id)
    for record in records:
        values = _values(record, key)
        rows = (await connection.execute(text(_READBACK_SQL), values)).mappings().all()
        if not _exact(rows, values):
            raise ValueError("saved manual historical evidence does not match")
    return len(records)

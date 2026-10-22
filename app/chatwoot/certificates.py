"""Durable PDF-certificate inventory for the Chatwoot Agent Bot."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.certificate_documents import CertificateObjectRef, ParsedCertificatePdf
from app.chatwoot.certificate_requests import initialize_schema as initialize_requests_schema
from app.chatwoot.certificate_requests import record_request
from app.chatwoot.certificate_schema import SCHEMA_LOCK_KEY, initialize_schema
from app.config import settings
from app.store import CertificateClaimResult, StoredCertificate

POOL_AIDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ozon": ("Ozon", ("medicine_card", "hostel_3_nights")),
    "pyaterochka": ("Пятёрочка", ("food_card", "children_card")),
}


@dataclass(frozen=True, slots=True)
class CertificatePoolStock:
    slug: str
    provider: str
    available: int
    total: int
    nearest_expiry: datetime | None


@dataclass(frozen=True, slots=True)
class HistoricalCertificateIssuance:
    """Observed Chatwoot acceptance; never proof of document delivery or reading."""

    account_id: int
    contact_id: int
    conversation_id: int
    source_message_id: int
    accepted_at: datetime
    provider_slug: str | None = None
    aid_id: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.account_id, self.contact_id, self.conversation_id, self.source_message_id,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("historical issuance identifiers must be positive integers")
        if self.accepted_at.tzinfo is None or self.accepted_at.utcoffset() is None:
            raise ValueError("historical acceptance timestamp must include a timezone")
        if self.provider_slug is not None and self.provider_slug not in POOL_AIDS:
            raise ValueError("unknown historical certificate provider")
        known_aids = {aid for _, aids in POOL_AIDS.values() for aid in aids}
        if self.aid_id is not None and self.aid_id not in known_aids:
            raise ValueError("unknown historical certificate aid")
        if (self.provider_slug is not None and self.aid_id is not None
                and self.aid_id not in POOL_AIDS[self.provider_slug][1]):
            raise ValueError("historical certificate provider does not match aid")


def database_url(password: str) -> str:
    query = (
        {"ssl": settings.certificate_database_sslmode}
        if settings.certificate_database_sslmode else {}
    )
    return URL.create(
        "postgresql+asyncpg",
        username=settings.certificate_database_user,
        password=password,
        host=settings.certificate_database_host,
        port=settings.certificate_database_port,
        database=settings.certificate_database_name,
        query=query,
    ).render_as_string(hide_password=False)


class CertificateInventory:
    def __init__(self, database_url: str, *, identity_key: str, account_id: int) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._identity_key = identity_key.encode()
        self._account_id = account_id

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": SCHEMA_LOCK_KEY}
            )
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS women_help_certificate_pools (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    slug VARCHAR(64) NOT NULL UNIQUE,
                    provider VARCHAR(128) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS women_help_certificate_pool_aids (
                    pool_id BIGINT NOT NULL REFERENCES women_help_certificate_pools(id)
                        ON DELETE CASCADE,
                    aid_id VARCHAR(64) NOT NULL,
                    PRIMARY KEY (pool_id, aid_id)
                )
            """))
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS women_help_pdf_certificates (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    pool_id BIGINT NOT NULL REFERENCES women_help_certificate_pools(id),
                    nominal_rubles INTEGER NOT NULL CHECK (nominal_rubles > 0),
                    activation_code TEXT NOT NULL UNIQUE,
                    serial_number VARCHAR(128) NOT NULL UNIQUE,
                    valid_from TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ NOT NULL,
                    pdf_bucket VARCHAR(128) NOT NULL,
                    pdf_object_key TEXT NOT NULL UNIQUE,
                    pdf_version_id TEXT,
                    pdf_sha256 VARCHAR(64) NOT NULL UNIQUE,
                    pdf_size INTEGER NOT NULL CHECK (pdf_size > 0),
                    pdf_filename VARCHAR(128) NOT NULL,
                    is_test BOOLEAN NOT NULL DEFAULT false,
                    status VARCHAR(16) NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available','reserved','submitted','delivered','failed')),
                    selected_aid_id VARCHAR(64),
                    issuance_key VARCHAR(128) UNIQUE,
                    recipient_hash VARCHAR(64),
                    chatwoot_message_id BIGINT,
                    reserved_at TIMESTAMPTZ,
                    submitted_at TIMESTAMPTZ,
                    delivered_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))
            await connection.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_women_help_pdf_certificates_recipient
                ON women_help_pdf_certificates (recipient_hash)
                WHERE recipient_hash IS NOT NULL
            """))
            await connection.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_women_help_pdf_certificates_available
                ON women_help_pdf_certificates (pool_id, expires_at, id)
                WHERE status = 'available'
            """))
            await initialize_schema(connection)
            for slug, (provider, aid_ids) in POOL_AIDS.items():
                pool_id = (await connection.execute(text("""
                    INSERT INTO women_help_certificate_pools (slug, provider)
                    VALUES (:slug, :provider)
                    ON CONFLICT (slug) DO UPDATE SET provider = EXCLUDED.provider
                    RETURNING id
                """), {"slug": slug, "provider": provider})).scalar_one()
                for aid_id in aid_ids:
                    await connection.execute(text("""
                        INSERT INTO women_help_certificate_pool_aids (pool_id, aid_id)
                        VALUES (:pool_id, :aid_id) ON CONFLICT DO NOTHING
                    """), {"pool_id": pool_id, "aid_id": aid_id})
            await initialize_requests_schema(connection)

    def _recipient_hash(self, recipient_id: int) -> str:
        identity = f"{self._account_id}:{recipient_id}".encode()
        return hmac.new(self._identity_key, identity, hashlib.sha256).hexdigest()

    async def record_request(
        self, aid_id: str, request_key: str, recipient_id: int,
        conversation_id: int, source_message_id: int, reason: str,
    ) -> bool:
        """Persist certificate demand without reserving or sending a document."""
        recipient_hash = self._recipient_hash(recipient_id)
        lock_key = int.from_bytes(bytes.fromhex(recipient_hash[:16]), "big", signed=True)
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
            )
            return await record_request(
                connection, account_id=self._account_id, recipient_hash=recipient_hash,
                contact_id=recipient_id, aid_id=aid_id, request_key=request_key,
                conversation_id=conversation_id, source_message_id=source_message_id,
                reason=reason,
            )

    async def claim(
        self, aid_id: str, issuance_key: str, recipient_id: int
    ) -> CertificateClaimResult:
        """Reserve one certificate total per Chatwoot contact."""
        recipient_hash = self._recipient_hash(recipient_id)
        lock_key = int.from_bytes(bytes.fromhex(recipient_hash[:16]), "big", signed=True)
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
            )
            params = {"issuance_key": issuance_key, "recipient_hash": recipient_hash}
            existing = await self._existing_claim(connection, params)
            if existing is not None:
                return existing
            row = (await connection.execute(text("""
                SELECT c.id FROM women_help_pdf_certificates c
                JOIN women_help_certificate_pool_aids a ON a.pool_id = c.pool_id
                WHERE a.aid_id = :aid_id AND c.record_kind = 'certificate'
                  AND c.status = 'available'
                  AND (c.valid_from IS NULL OR c.valid_from <= now())
                  AND c.expires_at > now()
                  AND (c.is_test OR (c.nominal_rubles = 3000
                       AND c.expires_at >= now() + interval '7 days'))
                ORDER BY c.expires_at, c.id
                FOR UPDATE OF c SKIP LOCKED LIMIT 1
            """), {"aid_id": aid_id})).first()
            if row is None:
                return CertificateClaimResult("unavailable")
            issued = (await connection.execute(text("""
                UPDATE women_help_pdf_certificates c
                SET status = 'reserved', selected_aid_id = :aid_id,
                    issuance_key = :issuance_key, recipient_hash = :recipient_hash,
                    reserved_at = now()
                FROM women_help_certificate_pools p
                WHERE c.id = :id AND p.id = c.pool_id
                RETURNING c.*, p.provider
            """), {"id": row.id, "aid_id": aid_id, **params})).mappings().one()
            return CertificateClaimResult("issued", _certificate(issued))

    async def resume(
        self, aid_id: str, issuance_key: str, recipient_id: int,
    ) -> CertificateClaimResult:
        """Read an existing delivery without reserving any new inventory."""
        params = {"issuance_key": issuance_key,
                  "recipient_hash": self._recipient_hash(recipient_id)}
        async with self._engine.connect() as connection:
            existing = await self._existing_claim(connection, params, exact_only=True)
            return existing or CertificateClaimResult("unavailable")

    async def _existing_claim(
        self, connection, params: dict[str, str], *, exact_only: bool = False,
    ) -> CertificateClaimResult | None:
        existing = (await connection.execute(text("""
            SELECT c.*, p.provider FROM women_help_pdf_certificates c
            JOIN women_help_certificate_pools p ON p.id = c.pool_id
            WHERE c.issuance_key = :issuance_key AND c.recipient_hash = :recipient_hash
              AND c.record_kind = 'certificate'
        """), params)).mappings().first()
        if existing is not None:
            return CertificateClaimResult("issued", _certificate(existing))
        if exact_only:
            return None
        prior = (await connection.execute(text("""
            SELECT c.*, p.provider FROM women_help_pdf_certificates c
            LEFT JOIN women_help_certificate_pools p ON p.id = c.pool_id
            WHERE c.recipient_hash = :recipient_hash LIMIT 1
        """), params)).mappings().first()
        if prior is not None:
            if prior["record_kind"] == "historical_issuance":
                return CertificateClaimResult("already_issued")
            # A transport failure must reuse the existing bearer document.
            if prior["status"] in {"reserved", "failed"}:
                return CertificateClaimResult("issued", _certificate(prior))
            return CertificateClaimResult("already_issued")
        return None

    async def mark_submitted(self, issuance_key: str, message_id: int) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates
                SET status = CASE WHEN status = 'delivered' THEN status ELSE 'submitted' END,
                    chatwoot_message_id = :message_id,
                    submitted_at = COALESCE(submitted_at, now())
                WHERE issuance_key = :issuance_key AND record_kind = 'certificate'
                  AND status IN ('reserved','submitted','delivered','failed')
            """), {"issuance_key": issuance_key, "message_id": message_id})

    async def mark_delivered(self, message_id: int) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates
                SET status = 'delivered', delivered_at = COALESCE(delivered_at, now())
                WHERE chatwoot_message_id = :message_id AND record_kind = 'certificate'
                  AND status IN ('submitted','delivered','failed')
            """), {"message_id": message_id})

    async def mark_delivery_failed(self, message_id: int) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates SET status = 'failed'
                WHERE chatwoot_message_id = :message_id AND record_kind = 'certificate'
                  AND status = 'submitted'
            """), {"message_id": message_id})

    async def mark_failed(self, issuance_key: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates SET status = 'failed'
                WHERE issuance_key = :issuance_key AND record_kind = 'certificate'
                  AND status = 'reserved'
            """), {"issuance_key": issuance_key})

    async def import_historical_issuances(
        self, issuances: Iterable[HistoricalCertificateIssuance],
    ) -> tuple[int, int]:
        """Atomically import evidence, accepting only exact repeats of prior imports."""
        records = tuple(issuances)
        if any(record.account_id != self._account_id for record in records):
            raise ValueError("historical issuance belongs to another Chatwoot account")
        imported = duplicates = 0
        async with self._engine.begin() as connection:
            # Same lock as claim(), ordered for repeatable multi-recipient imports.
            hashes = {self._recipient_hash(record.contact_id) for record in records}
            for lock_key in sorted(
                int.from_bytes(bytes.fromhex(value[:16]), "big", signed=True) for value in hashes
            ):
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
                )
            for record in records:
                values = {
                    "account_id": record.account_id, "contact_id": record.contact_id,
                    "conversation_id": record.conversation_id,
                    "source_message_id": record.source_message_id,
                    "accepted_at": record.accepted_at, "provider_slug": record.provider_slug,
                    "selected_aid_id": record.aid_id,
                    "recipient_hash": self._recipient_hash(record.contact_id),
                }
                prior = (await connection.execute(text("""
                    SELECT c.*, p.slug AS provider_slug
                    FROM women_help_pdf_certificates c
                    LEFT JOIN women_help_certificate_pools p ON p.id = c.pool_id
                    WHERE c.recipient_hash = :recipient_hash
                       OR (c.account_id = :account_id AND c.source_message_id = :source_message_id)
                    FOR UPDATE OF c
                """), values)).mappings().all()
                if prior:
                    if (len(prior) != 1 or prior[0]["record_kind"] != "historical_issuance"
                            or any(prior[0][key] != value for key, value in values.items())):
                        raise ValueError("historical issuance conflicts with existing inventory")
                    duplicates += 1
                    continue
                result = await connection.execute(text("""
                    INSERT INTO women_help_pdf_certificates
                        (record_kind, status, pool_id, recipient_hash, account_id, contact_id,
                         conversation_id, source_message_id, accepted_at, selected_aid_id)
                    SELECT 'historical_issuance', 'accepted', p.id, :recipient_hash,
                           :account_id, :contact_id, :conversation_id, :source_message_id,
                           :accepted_at, :selected_aid_id
                    FROM (SELECT 1) AS evidence
                    LEFT JOIN women_help_certificate_pools p ON p.slug = :provider_slug
                    WHERE :provider_slug IS NULL OR p.id IS NOT NULL
                    ON CONFLICT DO NOTHING RETURNING id
                """), values)
                if result.first() is None:
                    raise ValueError("historical issuance conflicts with existing inventory")
                imported += 1
            # Requests may have arrived between deploying tracking and backfill.
            # Reconcile them under the same locks and transaction as the history.
            for recipient_hash in sorted(hashes):
                await connection.execute(text("""
                    UPDATE women_help_certificate_requests r
                    SET status = 'fulfilled', fulfilled_certificate_id = c.id
                    FROM women_help_pdf_certificates c
                    WHERE r.recipient_hash = :recipient_hash AND r.status = 'waiting'
                      AND c.recipient_hash = r.recipient_hash
                      AND c.record_kind = 'historical_issuance'
                """), {"recipient_hash": recipient_hash})
        return imported, duplicates

    async def import_documents(
        self, documents: Iterable[tuple[ParsedCertificatePdf, CertificateObjectRef]]
    ) -> tuple[int, int]:
        imported = duplicates = 0
        async with self._engine.begin() as connection:
            for parsed, ref in documents:
                values = {
                    "provider_slug": parsed.provider_slug,
                    "nominal_rubles": parsed.nominal_rubles,
                    "activation_code": parsed.activation_code,
                    "serial_number": parsed.serial_number,
                    "valid_from": parsed.valid_from,
                    "expires_at": parsed.expires_at,
                    "pdf_bucket": ref.bucket,
                    "pdf_object_key": ref.key,
                    "pdf_version_id": ref.version_id,
                    "pdf_sha256": ref.sha256,
                    "pdf_size": ref.size,
                    "pdf_filename": parsed.filename,
                    "is_test": parsed.is_test,
                }
                result = await connection.execute(text("""
                    INSERT INTO women_help_pdf_certificates
                        (pool_id, nominal_rubles, activation_code, serial_number,
                         valid_from, expires_at, pdf_bucket, pdf_object_key,
                         pdf_version_id, pdf_sha256, pdf_size, pdf_filename, is_test)
                    SELECT id, :nominal_rubles, :activation_code, :serial_number,
                           :valid_from, :expires_at, :pdf_bucket, :pdf_object_key,
                           :pdf_version_id, :pdf_sha256, :pdf_size, :pdf_filename, :is_test
                    FROM women_help_certificate_pools WHERE slug = :provider_slug
                    ON CONFLICT DO NOTHING RETURNING id
                """), values)
                if result.first() is not None:
                    imported += 1
                else:
                    raise ValueError("certificate already exists or conflicts with inventory")
        return imported, duplicates

    async def import_admin_batch(
        self,
        documents: Iterable[tuple[ParsedCertificatePdf, CertificateObjectRef]],
        *,
        batch_id: int,
        admin_id: int,
    ) -> int:
        """Commit inventory rows and their durable admin-batch receipt together."""
        imported = 0
        async with self._engine.begin() as connection:
            batch = (await connection.execute(text("""
                SELECT 1 FROM women_help_certificate_import_batches
                WHERE id = :batch_id AND admin_id = :admin_id
                  AND status = 'awaiting_confirmation' AND expires_at > now()
                FOR UPDATE
            """), {"batch_id": batch_id, "admin_id": admin_id})).first()
            if batch is None:
                raise ValueError("certificate import batch is not confirmable")
            for parsed, ref in documents:
                result = await connection.execute(text("""
                    INSERT INTO women_help_pdf_certificates
                        (pool_id, nominal_rubles, activation_code, serial_number,
                         valid_from, expires_at, pdf_bucket, pdf_object_key,
                         pdf_version_id, pdf_sha256, pdf_size, pdf_filename, is_test)
                    SELECT id, :nominal_rubles, :activation_code, :serial_number,
                           :valid_from, :expires_at, :pdf_bucket, :pdf_object_key,
                           :pdf_version_id, :pdf_sha256, :pdf_size, :pdf_filename, :is_test
                    FROM women_help_certificate_pools WHERE slug = :provider_slug
                    ON CONFLICT DO NOTHING RETURNING id
                """), {
                    "provider_slug": parsed.provider_slug,
                    "nominal_rubles": parsed.nominal_rubles,
                    "activation_code": parsed.activation_code,
                    "serial_number": parsed.serial_number,
                    "valid_from": parsed.valid_from,
                    "expires_at": parsed.expires_at,
                    "pdf_bucket": ref.bucket,
                    "pdf_object_key": ref.key,
                    "pdf_version_id": ref.version_id,
                    "pdf_sha256": ref.sha256,
                    "pdf_size": ref.size,
                    "pdf_filename": parsed.filename,
                    "is_test": parsed.is_test,
                })
                if result.first() is None:
                    raise ValueError("certificate already exists or conflicts with inventory")
                imported += 1
            await connection.execute(text("""
                UPDATE women_help_certificate_import_batches
                SET status = 'imported', confirmed_at = now(), updated_at = now()
                WHERE id = :batch_id
            """), {"batch_id": batch_id})
            await connection.execute(text("""
                UPDATE women_help_certificate_import_items
                SET status = 'imported', activation_code = NULL, serial_number = NULL,
                    pdf_bucket = NULL, pdf_object_key = NULL, pdf_version_id = NULL
                WHERE batch_id = :batch_id AND status = 'ready'
            """), {"batch_id": batch_id})
        return imported

    async def contains_document(self, parsed: ParsedCertificatePdf) -> bool:
        """Check all inventory identities without exposing any bearer value."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(text("""
                SELECT 1 FROM women_help_pdf_certificates
                WHERE record_kind = 'certificate' AND (activation_code = :activation_code
                   OR serial_number = :serial_number
                   OR pdf_sha256 = :pdf_sha256)
                LIMIT 1
            """), {
                "activation_code": parsed.activation_code,
                "serial_number": parsed.serial_number,
                "pdf_sha256": parsed.pdf_sha256,
            })).first()
            return row is not None

    async def stock(self) -> tuple[CertificatePoolStock, ...]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(text("""
                SELECT p.slug, p.provider,
                    count(c.id) FILTER (
                        WHERE c.status = 'available'
                          AND (c.valid_from IS NULL OR c.valid_from <= now())
                          AND c.expires_at > now()
                    )::int AS available,
                    count(c.id)::int AS total,
                    min(c.expires_at) FILTER (
                        WHERE c.status = 'available'
                          AND (c.valid_from IS NULL OR c.valid_from <= now())
                          AND c.expires_at > now()
                    ) AS nearest_expiry
                FROM women_help_certificate_pools p
                LEFT JOIN women_help_pdf_certificates c
                    ON c.pool_id = p.id AND c.record_kind = 'certificate'
                GROUP BY p.id, p.slug, p.provider ORDER BY p.slug
            """))).mappings()
            return tuple(CertificatePoolStock(**row) for row in rows)

    async def reset_test(self) -> int:
        async with self._engine.begin() as connection:
            result = await connection.execute(text("""
                UPDATE women_help_pdf_certificates
                SET status = 'available', selected_aid_id = NULL, issuance_key = NULL,
                    recipient_hash = NULL, chatwoot_message_id = NULL,
                    reserved_at = NULL, submitted_at = NULL, delivered_at = NULL
                WHERE record_kind = 'certificate' AND is_test = true AND status <> 'available'
            """))
            return result.rowcount

    async def test_object_refs(self) -> tuple[CertificateObjectRef, ...]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(text("""
                SELECT pdf_bucket, pdf_object_key, pdf_version_id, pdf_sha256, pdf_size
                FROM women_help_pdf_certificates
                WHERE record_kind = 'certificate' AND is_test = true
            """))).mappings()
            return tuple(CertificateObjectRef(
                bucket=row["pdf_bucket"], key=row["pdf_object_key"],
                version_id=row["pdf_version_id"], sha256=row["pdf_sha256"],
                size=row["pdf_size"],
            ) for row in rows)

    async def purge_test_records(self) -> int:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("""DELETE FROM women_help_pdf_certificates
                        WHERE record_kind = 'certificate' AND is_test = true""")
            )
            return result.rowcount

    async def purge_legacy_test_inventory(self) -> None:
        """Drop superseded JSON-only test data after the explicit migration command."""
        async with self._engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS women_help_certificates"))

    async def close(self) -> None:
        await self._engine.dispose()


def _certificate(row: Any) -> StoredCertificate:
    return StoredCertificate(
        aid_id=row["selected_aid_id"], provider=row["provider"],
        nominal_rubles=row["nominal_rubles"], activation_code=row["activation_code"],
        serial_number=row["serial_number"], valid_from=row["valid_from"],
        expires_at=row["expires_at"], issued_at=row["reserved_at"],
        issuance_key=row["issuance_key"], pdf_bucket=row["pdf_bucket"],
        pdf_object_key=row["pdf_object_key"], pdf_version_id=row["pdf_version_id"],
        pdf_sha256=row["pdf_sha256"], pdf_size=row["pdf_size"],
        pdf_filename=row["pdf_filename"], is_test=row["is_test"],
    )

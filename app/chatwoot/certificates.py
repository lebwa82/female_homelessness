"""Durable PDF-certificate inventory for the Chatwoot Agent Bot."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.certificate_documents import CertificateObjectRef, ParsedCertificatePdf
from app.store import CertificateClaimResult, StoredCertificate

POOL_AIDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ozon": ("Ozon", ("medicine_card", "hostel_3_nights")),
    "pyaterochka": ("Пятёрочка", ("food_card", "children_card")),
}


def database_url(password: str) -> str:
    return URL.create(
        "postgresql+asyncpg", username="chatwoot", password=password,
        host="postgres", port=5432, database="chatwoot",
    ).render_as_string(hide_password=False)


class CertificateInventory:
    def __init__(self, database_url: str, *, identity_key: str, account_id: int) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._identity_key = identity_key.encode()
        self._account_id = account_id

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
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

    def _recipient_hash(self, recipient_id: int) -> str:
        identity = f"{self._account_id}:{recipient_id}".encode()
        return hmac.new(self._identity_key, identity, hashlib.sha256).hexdigest()

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
            existing = (await connection.execute(text("""
                SELECT c.*, p.provider FROM women_help_pdf_certificates c
                JOIN women_help_certificate_pools p ON p.id = c.pool_id
                WHERE c.issuance_key = :issuance_key AND c.recipient_hash = :recipient_hash
            """), params)).mappings().first()
            if existing is not None:
                return CertificateClaimResult("issued", _certificate(existing))
            prior = (await connection.execute(text("""
                SELECT c.*, p.provider FROM women_help_pdf_certificates c
                JOIN women_help_certificate_pools p ON p.id = c.pool_id
                WHERE c.recipient_hash = :recipient_hash LIMIT 1
            """), params)).mappings().first()
            if prior is not None:
                # A transport failure must be retryable with the same bearer
                # document, never by taking another certificate from the pool.
                if prior["status"] in {"reserved", "failed"}:
                    return CertificateClaimResult("issued", _certificate(prior))
                return CertificateClaimResult("already_issued")
            row = (await connection.execute(text("""
                SELECT c.id FROM women_help_pdf_certificates c
                JOIN women_help_certificate_pool_aids a ON a.pool_id = c.pool_id
                WHERE a.aid_id = :aid_id AND c.status = 'available'
                  AND (c.valid_from IS NULL OR c.valid_from <= now())
                  AND c.expires_at > now()
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

    async def mark_submitted(self, issuance_key: str, message_id: int) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates
                SET status = CASE WHEN status = 'delivered' THEN status ELSE 'submitted' END,
                    chatwoot_message_id = :message_id,
                    submitted_at = COALESCE(submitted_at, now())
                WHERE issuance_key = :issuance_key
                  AND status IN ('reserved','submitted','delivered','failed')
            """), {"issuance_key": issuance_key, "message_id": message_id})

    async def mark_delivered(self, message_id: int) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates
                SET status = 'delivered', delivered_at = COALESCE(delivered_at, now())
                WHERE chatwoot_message_id = :message_id
                  AND status IN ('submitted','delivered','failed')
            """), {"message_id": message_id})

    async def mark_delivery_failed(self, message_id: int) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates SET status = 'failed'
                WHERE chatwoot_message_id = :message_id AND status = 'submitted'
            """), {"message_id": message_id})

    async def mark_failed(self, issuance_key: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_pdf_certificates SET status = 'failed'
                WHERE issuance_key = :issuance_key AND status = 'reserved'
            """), {"issuance_key": issuance_key})

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

    async def reset_test(self) -> int:
        async with self._engine.begin() as connection:
            result = await connection.execute(text("""
                UPDATE women_help_pdf_certificates
                SET status = 'available', selected_aid_id = NULL, issuance_key = NULL,
                    recipient_hash = NULL, chatwoot_message_id = NULL,
                    reserved_at = NULL, submitted_at = NULL, delivered_at = NULL
                WHERE is_test = true AND status <> 'available'
            """))
            return result.rowcount

    async def test_object_refs(self) -> tuple[CertificateObjectRef, ...]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(text("""
                SELECT pdf_bucket, pdf_object_key, pdf_version_id, pdf_sha256, pdf_size
                FROM women_help_pdf_certificates WHERE is_test = true
            """))).mappings()
            return tuple(CertificateObjectRef(
                bucket=row["pdf_bucket"], key=row["pdf_object_key"],
                version_id=row["pdf_version_id"], sha256=row["pdf_sha256"],
                size=row["pdf_size"],
            ) for row in rows)

    async def purge_test_records(self) -> int:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("DELETE FROM women_help_pdf_certificates WHERE is_test = true")
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

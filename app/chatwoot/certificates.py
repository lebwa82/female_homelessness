"""Durable, one-way certificate inventory for the Chatwoot Agent Bot."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.store import CertificateClaimResult, StoredCertificate


def database_url(password: str) -> str:
    return URL.create(
        "postgresql+asyncpg",
        username="chatwoot",
        password=password,
        host="postgres",
        port=5432,
        database="chatwoot",
    ).render_as_string(hide_password=False)


class CertificateInventory:
    def __init__(self, database_url: str, *, identity_key: str, account_id: int) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._identity_key = identity_key.encode()
        self._account_id = account_id

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text("""
                CREATE TABLE IF NOT EXISTS women_help_certificates (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    aid_id VARCHAR(64) NOT NULL,
                    provider VARCHAR(128) NOT NULL,
                    nominal_rubles INTEGER NOT NULL CHECK (nominal_rubles > 0),
                    activation_code TEXT NOT NULL UNIQUE,
                    expires_at TIMESTAMPTZ NOT NULL,
                    serial_number VARCHAR(128) NOT NULL UNIQUE,
                    issuance_key VARCHAR(128) UNIQUE,
                    recipient_hash VARCHAR(64),
                    issued_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            )
            await connection.execute(text("""
                ALTER TABLE women_help_certificates
                ADD COLUMN IF NOT EXISTS recipient_hash VARCHAR(64)
            """))
            await connection.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_women_help_certificates_recipient
                ON women_help_certificates (recipient_hash)
                WHERE recipient_hash IS NOT NULL
            """))
            await connection.execute(
                text("""
                CREATE INDEX IF NOT EXISTS ix_women_help_certificates_available
                ON women_help_certificates (aid_id, expires_at, id)
                WHERE issued_at IS NULL
            """)
            )

    def _recipient_hash(self, recipient_id: int) -> str:
        identity = f"{self._account_id}:{recipient_id}".encode()
        return hmac.new(self._identity_key, identity, hashlib.sha256).hexdigest()

    async def claim(
        self, aid_id: str, issuance_key: str, recipient_id: int
    ) -> CertificateClaimResult:
        """One certificate across all aid types, durable across /clear and conversations."""
        recipient_hash = self._recipient_hash(recipient_id)
        lock_key = int.from_bytes(bytes.fromhex(recipient_hash[:16]), "big", signed=True)
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
            )
            existing = (
                (
                    await connection.execute(
                        text("""
                            SELECT aid_id, provider, nominal_rubles, activation_code,
                                   expires_at, serial_number, issued_at
                            FROM women_help_certificates
                            WHERE issuance_key = :issuance_key AND recipient_hash = :recipient_hash
                        """), {"issuance_key": issuance_key, "recipient_hash": recipient_hash}
                    )
                ).mappings().first()
            )
            if existing is not None:
                return CertificateClaimResult("issued", _certificate(existing))
            previously_issued = (await connection.execute(
                text("""
                    SELECT 1 FROM women_help_certificates
                    WHERE recipient_hash = :recipient_hash LIMIT 1
                """), {"recipient_hash": recipient_hash}
            )).first()
            if previously_issued is not None:
                return CertificateClaimResult("already_issued")
            legacy_unlinked = (await connection.execute(
                text("""
                    SELECT 1 FROM women_help_certificates
                    WHERE issued_at IS NOT NULL AND recipient_hash IS NULL LIMIT 1
                """)
            )).first()
            if legacy_unlinked is not None:
                # A previous recipient cannot be identified reliably from this
                # inventory alone. Never silently issue another code to them.
                return CertificateClaimResult("review_required")
            row = (await connection.execute(
                text("""
                    SELECT id FROM women_help_certificates
                    WHERE aid_id = :aid_id AND issued_at IS NULL AND expires_at > now()
                    ORDER BY expires_at, id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                """), {"aid_id": aid_id}
            )).first()
            if row is None:
                return CertificateClaimResult("unavailable")
            issued = (
                (
                    await connection.execute(
                        text("""
                            UPDATE women_help_certificates
                            SET issuance_key = :issuance_key,
                                recipient_hash = :recipient_hash, issued_at = now()
                            WHERE id = :id
                            RETURNING aid_id, provider, nominal_rubles, activation_code,
                                      expires_at, serial_number, issued_at
                        """), {"id": row.id, "issuance_key": issuance_key,
                               "recipient_hash": recipient_hash}
                    )
                ).mappings().one()
            )
            return CertificateClaimResult("issued", _certificate(issued))

    async def import_rows(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Import validated rows without logging bearer values."""
        imported = duplicates = 0
        async with self._engine.begin() as connection:
            for row in rows:
                result = await connection.execute(
                    text("""
                    INSERT INTO women_help_certificates
                        (aid_id, provider, nominal_rubles, activation_code,
                         expires_at, serial_number)
                    VALUES (:aid_id, :provider, :nominal_rubles, :activation_code,
                            :expires_at, :serial_number)
                    ON CONFLICT DO NOTHING RETURNING id
                """),
                    row,
                )
                if result.first() is not None:
                    imported += 1
                    continue
                existing = (
                    (
                        await connection.execute(
                            text("""
                    SELECT aid_id, provider, nominal_rubles, activation_code,
                           expires_at, serial_number
                    FROM women_help_certificates
                    WHERE activation_code = :activation_code OR serial_number = :serial_number
                """),
                            row,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is None or any(existing[key] != row[key] for key in row):
                    raise ValueError("conflicting activation code or serial number")
                duplicates += 1
        return imported, duplicates

    async def link_legacy(self, serial_number: str, recipient_id: int) -> bool:
        """Associate a pre-limit issuance with a verified Chatwoot contact."""
        recipient_hash = self._recipient_hash(recipient_id)
        lock_key = int.from_bytes(bytes.fromhex(recipient_hash[:16]), "big", signed=True)
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
            )
            row = (await connection.execute(
                text("""
                    SELECT id, recipient_hash, issued_at
                    FROM women_help_certificates
                    WHERE serial_number = :serial_number FOR UPDATE
                """), {"serial_number": serial_number}
            )).mappings().first()
            if row is None or row["issued_at"] is None:
                raise ValueError("historical issued certificate not found")
            if row["recipient_hash"] == recipient_hash:
                return False
            if row["recipient_hash"] is not None:
                raise ValueError("historical certificate belongs to another contact")
            existing = (await connection.execute(
                text("""
                    SELECT 1 FROM women_help_certificates
                    WHERE recipient_hash = :recipient_hash LIMIT 1
                """), {"recipient_hash": recipient_hash}
            )).first()
            if existing is not None:
                raise ValueError("contact already has a certificate")
            await connection.execute(
                text("""
                    UPDATE women_help_certificates
                    SET recipient_hash = :recipient_hash WHERE id = :id
                """), {"recipient_hash": recipient_hash, "id": row["id"]}
            )
            return True

    async def unlinked_count(self) -> int:
        async with self._engine.connect() as connection:
            return int((await connection.execute(text("""
                SELECT count(*) FROM women_help_certificates
                WHERE issued_at IS NOT NULL AND recipient_hash IS NULL
            """))).scalar_one())

    async def close(self) -> None:
        await self._engine.dispose()


def _certificate(row: Any) -> StoredCertificate:
    return StoredCertificate(
        aid_id=row["aid_id"],
        provider=row["provider"],
        nominal_rubles=row["nominal_rubles"],
        activation_code=row["activation_code"],
        expires_at=row["expires_at"],
        serial_number=row["serial_number"],
        issued_at=row["issued_at"],
    )

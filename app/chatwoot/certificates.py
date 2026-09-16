"""Durable, one-way certificate inventory for the Chatwoot Agent Bot."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.store import StoredCertificate


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
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)

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
                    issued_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            )
            await connection.execute(
                text("""
                CREATE INDEX IF NOT EXISTS ix_women_help_certificates_available
                ON women_help_certificates (aid_id, expires_at, id)
                WHERE issued_at IS NULL
            """)
            )

    async def claim(self, aid_id: str, issuance_key: str) -> StoredCertificate | None:
        """Atomically reserve one valid code; a retry with the same key gets that code."""
        for attempt in range(2):
            try:
                async with self._engine.begin() as connection:
                    existing = (
                        (
                            await connection.execute(
                                text("""
                            SELECT aid_id, provider, nominal_rubles, activation_code,
                                   expires_at, serial_number, issued_at
                            FROM women_help_certificates WHERE issuance_key = :issuance_key
                        """),
                                {"issuance_key": issuance_key},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if existing is not None:
                        return _certificate(existing)
                    row = (
                        await connection.execute(
                            text("""
                            SELECT id FROM women_help_certificates
                            WHERE aid_id = :aid_id AND issued_at IS NULL AND expires_at > now()
                            ORDER BY expires_at, id
                            FOR UPDATE SKIP LOCKED LIMIT 1
                        """),
                            {"aid_id": aid_id},
                        )
                    ).first()
                    if row is None:
                        return None
                    issued = (
                        (
                            await connection.execute(
                                text("""
                            UPDATE women_help_certificates
                            SET issuance_key = :issuance_key, issued_at = now()
                            WHERE id = :id
                            RETURNING aid_id, provider, nominal_rubles, activation_code,
                                      expires_at, serial_number, issued_at
                        """),
                                {"id": row.id, "issuance_key": issuance_key},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    return _certificate(issued)
            except IntegrityError:
                # A concurrent retry may have claimed a different row with the
                # same key. Its transaction will now be visible to the next read.
                if attempt:
                    raise
        return None

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

"""Durable administration state for the private certificate Telegram bot."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.certificate_documents import CertificateObjectRef, ParsedCertificatePdf

ADMIN_ROLES = frozenset({"owner", "operator"})
ACTIVE_BATCH_STATUSES = ("collecting", "awaiting_confirmation")


@dataclass(frozen=True, slots=True)
class CertificateAdmin:
    telegram_user_id: int
    display_name: str
    role: str
    is_bootstrap: bool


@dataclass(frozen=True, slots=True)
class InviteClaim:
    invite_id: int
    created_by: int
    role: str


@dataclass(frozen=True, slots=True)
class ImportBatch:
    id: int
    admin_id: int
    pool_slug: str
    status: str
    target_count: int


@dataclass(frozen=True, slots=True)
class BatchSummary:
    batch_id: int
    pool_slug: str
    status: str
    target_count: int
    ready: int
    duplicates: int
    invalid: int

    @property
    def received(self) -> int:
        return self.ready + self.duplicates + self.invalid


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class CertificateAdminRepository:
    """Keep admin access and upload batches in the existing Chatwoot PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)

    async def initialize(self, bootstrap_owner_ids: tuple[int, ...]) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS women_help_certificate_admins (
                    telegram_user_id BIGINT PRIMARY KEY,
                    display_name VARCHAR(160) NOT NULL,
                    role VARCHAR(16) NOT NULL CHECK (role IN ('owner','operator')),
                    is_active BOOLEAN NOT NULL DEFAULT true,
                    is_bootstrap BOOLEAN NOT NULL DEFAULT false,
                    added_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS women_help_certificate_admin_invites (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    token_hash VARCHAR(64) NOT NULL UNIQUE,
                    role VARCHAR(16) NOT NULL CHECK (role IN ('owner','operator')),
                    created_by BIGINT NOT NULL,
                    candidate_id BIGINT,
                    candidate_name VARCHAR(160),
                    status VARCHAR(16) NOT NULL DEFAULT 'invited'
                        CHECK (status IN ('invited','claimed','approved','rejected')),
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    claimed_at TIMESTAMPTZ,
                    resolved_at TIMESTAMPTZ
                )
            """))
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS women_help_certificate_import_batches (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    admin_id BIGINT NOT NULL,
                    pool_slug VARCHAR(64) NOT NULL CHECK (pool_slug IN ('ozon','pyaterochka')),
                    status VARCHAR(32) NOT NULL DEFAULT 'collecting'
                        CHECK (status IN ('collecting','awaiting_confirmation','imported',
                                         'cancelled','expired','failed')),
                    target_count INTEGER NOT NULL CHECK (target_count > 0),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at TIMESTAMPTZ NOT NULL,
                    confirmed_at TIMESTAMPTZ
                )
            """))
            await connection.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_women_help_certificate_active_batch
                ON women_help_certificate_import_batches (admin_id)
                WHERE status IN ('collecting','awaiting_confirmation')
            """))
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS women_help_certificate_import_items (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    batch_id BIGINT NOT NULL REFERENCES women_help_certificate_import_batches(id)
                        ON DELETE CASCADE,
                    original_filename VARCHAR(256) NOT NULL,
                    status VARCHAR(16) NOT NULL
                        CHECK (status IN ('ready','duplicate','invalid','imported')),
                    error_code VARCHAR(64),
                    nominal_rubles INTEGER,
                    activation_code TEXT,
                    serial_number VARCHAR(128),
                    valid_from TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ,
                    pdf_bucket VARCHAR(128),
                    pdf_object_key TEXT,
                    pdf_version_id TEXT,
                    pdf_sha256 VARCHAR(64),
                    pdf_size INTEGER,
                    pdf_filename VARCHAR(128),
                    is_test BOOLEAN,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))
            await connection.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_women_help_import_item_code
                ON women_help_certificate_import_items (batch_id, activation_code)
                WHERE activation_code IS NOT NULL
            """))
            await connection.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_women_help_import_item_serial
                ON women_help_certificate_import_items (batch_id, serial_number)
                WHERE serial_number IS NOT NULL
            """))
            await connection.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_women_help_import_item_pdf
                ON women_help_certificate_import_items (batch_id, pdf_sha256)
                WHERE pdf_sha256 IS NOT NULL
            """))
            for owner_id in bootstrap_owner_ids:
                await connection.execute(text("""
                    INSERT INTO women_help_certificate_admins
                        (telegram_user_id, display_name, role, is_active, is_bootstrap)
                    VALUES (:user_id, :display_name, 'owner', true, true)
                    ON CONFLICT (telegram_user_id) DO UPDATE
                    SET role = 'owner', is_active = true, is_bootstrap = true,
                        updated_at = now()
                """), {"user_id": owner_id, "display_name": f"Owner {owner_id}"})

    async def admin(self, user_id: int) -> CertificateAdmin | None:
        async with self._engine.connect() as connection:
            row = (await connection.execute(text("""
                SELECT telegram_user_id, display_name, role, is_bootstrap
                FROM women_help_certificate_admins
                WHERE telegram_user_id = :user_id AND is_active = true
            """), {"user_id": user_id})).mappings().first()
        return CertificateAdmin(**row) if row is not None else None

    async def claim_initial_owner(self, user_id: int, display_name: str) -> bool:
        """Atomically bind the configured bootstrap username to its Telegram ID once."""
        async with self._engine.begin() as connection:
            result = await connection.execute(text("""
                INSERT INTO women_help_certificate_admins
                    (telegram_user_id, display_name, role, is_active, is_bootstrap)
                SELECT :user_id, :display_name, 'owner', true, true
                WHERE NOT EXISTS (
                    SELECT 1 FROM women_help_certificate_admins WHERE is_active = true
                )
                ON CONFLICT (telegram_user_id) DO NOTHING
                RETURNING telegram_user_id
            """), {"user_id": user_id, "display_name": display_name[:160]})
            return result.first() is not None

    async def update_identity(self, user_id: int, display_name: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_certificate_admins
                SET display_name = :display_name, updated_at = now()
                WHERE telegram_user_id = :user_id AND is_active = true
            """), {"user_id": user_id, "display_name": display_name[:160]})

    async def admins(self) -> tuple[CertificateAdmin, ...]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(text("""
                SELECT telegram_user_id, display_name, role, is_bootstrap
                FROM women_help_certificate_admins WHERE is_active = true
                ORDER BY role DESC, created_at, telegram_user_id
            """))).mappings()
            return tuple(CertificateAdmin(**row) for row in rows)

    async def create_invite(self, owner_id: int, role: str, ttl_hours: int) -> tuple[int, str]:
        if role not in ADMIN_ROLES:
            raise ValueError("unsupported administrator role")
        token = secrets.token_urlsafe(24)
        async with self._engine.begin() as connection:
            row = (await connection.execute(text("""
                INSERT INTO women_help_certificate_admin_invites
                    (token_hash, role, created_by, expires_at)
                SELECT :token_hash, :role, :owner_id, :expires_at
                FROM women_help_certificate_admins
                WHERE telegram_user_id = :owner_id AND role = 'owner' AND is_active = true
                RETURNING id
            """), {
                "token_hash": _token_hash(token), "role": role, "owner_id": owner_id,
                "expires_at": datetime.now(UTC) + timedelta(hours=ttl_hours),
            })).first()
            if row is None:
                raise PermissionError("only an owner can invite administrators")
            return row.id, token

    async def claim_invite(
        self, token: str, candidate_id: int, candidate_name: str
    ) -> InviteClaim | None:
        async with self._engine.begin() as connection:
            row = (await connection.execute(text("""
                UPDATE women_help_certificate_admin_invites
                SET candidate_id = :candidate_id, candidate_name = :candidate_name,
                    status = 'claimed', claimed_at = now()
                WHERE token_hash = :token_hash AND status = 'invited' AND expires_at > now()
                RETURNING id AS invite_id, created_by, role
            """), {
                "token_hash": _token_hash(token), "candidate_id": candidate_id,
                "candidate_name": candidate_name[:160],
            })).mappings().first()
        return InviteClaim(**row) if row is not None else None

    async def resolve_invite(self, owner_id: int, invite_id: int, *, approve: bool) -> int | None:
        async with self._engine.begin() as connection:
            owner = (await connection.execute(text("""
                SELECT 1 FROM women_help_certificate_admins
                WHERE telegram_user_id = :owner_id AND role = 'owner' AND is_active = true
            """), {"owner_id": owner_id})).first()
            if owner is None:
                raise PermissionError("only an owner can resolve invitations")
            invite = (await connection.execute(text("""
                SELECT candidate_id, candidate_name, role
                FROM women_help_certificate_admin_invites
                WHERE id = :invite_id AND created_by = :owner_id AND status = 'claimed'
                  AND expires_at > now() FOR UPDATE
            """), {"invite_id": invite_id, "owner_id": owner_id})).mappings().first()
            if invite is None or invite["candidate_id"] is None:
                return None
            if approve:
                await connection.execute(text("""
                    INSERT INTO women_help_certificate_admins
                        (telegram_user_id, display_name, role, is_active, added_by)
                    VALUES (:candidate_id, :candidate_name, :role, true, :owner_id)
                    ON CONFLICT (telegram_user_id) DO UPDATE
                    SET display_name = EXCLUDED.display_name, role = EXCLUDED.role,
                        is_active = true, added_by = EXCLUDED.added_by, updated_at = now()
                """), {**invite, "owner_id": owner_id})
            await connection.execute(text("""
                UPDATE women_help_certificate_admin_invites
                SET status = :status, resolved_at = now() WHERE id = :invite_id
            """), {"status": "approved" if approve else "rejected", "invite_id": invite_id})
            return int(invite["candidate_id"])

    async def revoke(self, owner_id: int, target_id: int) -> bool:
        async with self._engine.begin() as connection:
            owner = (await connection.execute(text("""
                SELECT 1 FROM women_help_certificate_admins
                WHERE telegram_user_id = :owner_id AND role = 'owner' AND is_active = true
            """), {"owner_id": owner_id})).first()
            if owner is None:
                raise PermissionError("only an owner can revoke administrators")
            target = (await connection.execute(text("""
                SELECT role, is_bootstrap FROM women_help_certificate_admins
                WHERE telegram_user_id = :target_id AND is_active = true FOR UPDATE
            """), {"target_id": target_id})).mappings().first()
            if target is None or target["is_bootstrap"]:
                return False
            if target["role"] == "owner":
                owners = (await connection.execute(text("""
                    SELECT count(*) FROM women_help_certificate_admins
                    WHERE role = 'owner' AND is_active = true
                """))).scalar_one()
                if owners <= 1:
                    return False
            result = await connection.execute(text("""
                UPDATE women_help_certificate_admins SET is_active = false, updated_at = now()
                WHERE telegram_user_id = :target_id AND is_active = true
            """), {"target_id": target_id})
            return result.rowcount == 1

    async def start_batch(
        self, admin_id: int, pool_slug: str, *, target_count: int, ttl_hours: int
    ) -> ImportBatch:
        if pool_slug not in {"ozon", "pyaterochka"}:
            raise ValueError("unsupported certificate pool")
        async with self._engine.begin() as connection:
            row = (await connection.execute(text("""
                INSERT INTO women_help_certificate_import_batches
                    (admin_id, pool_slug, target_count, expires_at)
                SELECT :admin_id, :pool_slug, :target_count, :expires_at
                FROM women_help_certificate_admins
                WHERE telegram_user_id = :admin_id AND is_active = true
                RETURNING id, admin_id, pool_slug, status, target_count
            """), {
                "admin_id": admin_id, "pool_slug": pool_slug, "target_count": target_count,
                "expires_at": datetime.now(UTC) + timedelta(hours=ttl_hours),
            })).mappings().first()
            if row is None:
                raise PermissionError("administrator access is required")
            return ImportBatch(**row)

    async def active_batch(self, admin_id: int) -> ImportBatch | None:
        async with self._engine.connect() as connection:
            row = (await connection.execute(text("""
                SELECT id, admin_id, pool_slug, status, target_count
                FROM women_help_certificate_import_batches
                WHERE admin_id = :admin_id
                  AND status IN ('collecting','awaiting_confirmation')
                  AND expires_at > now()
                ORDER BY id DESC LIMIT 1
            """), {"admin_id": admin_id})).mappings().first()
        return ImportBatch(**row) if row is not None else None

    async def add_ready(
        self,
        batch_id: int,
        original_filename: str,
        parsed: ParsedCertificatePdf,
        ref: CertificateObjectRef,
    ) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(text("""
                INSERT INTO women_help_certificate_import_items
                    (batch_id, original_filename, status, nominal_rubles, activation_code,
                     serial_number, valid_from, expires_at, pdf_bucket, pdf_object_key,
                     pdf_version_id, pdf_sha256, pdf_size, pdf_filename, is_test)
                SELECT :batch_id, :original_filename, 'ready', :nominal_rubles,
                       :activation_code, :serial_number, :valid_from, :expires_at,
                       :pdf_bucket, :pdf_object_key, :pdf_version_id, :pdf_sha256,
                       :pdf_size, :pdf_filename, :is_test
                FROM women_help_certificate_import_batches
                WHERE id = :batch_id AND status = 'collecting' AND expires_at > now()
                ON CONFLICT DO NOTHING RETURNING id
            """), {
                "batch_id": batch_id, "original_filename": original_filename[:256],
                "nominal_rubles": parsed.nominal_rubles,
                "activation_code": parsed.activation_code, "serial_number": parsed.serial_number,
                "valid_from": parsed.valid_from, "expires_at": parsed.expires_at,
                "pdf_bucket": ref.bucket, "pdf_object_key": ref.key,
                "pdf_version_id": ref.version_id, "pdf_sha256": ref.sha256,
                "pdf_size": ref.size, "pdf_filename": parsed.filename,
                "is_test": parsed.is_test,
            })
            return result.first() is not None

    async def add_rejected(self, batch_id: int, filename: str, status: str, error_code: str) -> None:
        if status not in {"duplicate", "invalid"}:
            raise ValueError("unsupported rejected item status")
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO women_help_certificate_import_items
                    (batch_id, original_filename, status, error_code)
                SELECT :batch_id, :filename, :status, :error_code
                FROM women_help_certificate_import_batches
                WHERE id = :batch_id AND status = 'collecting' AND expires_at > now()
            """), {
                "batch_id": batch_id, "filename": filename[:256],
                "status": status, "error_code": error_code,
            })

    async def finish_batch(self, admin_id: int, batch_id: int) -> BatchSummary | None:
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE women_help_certificate_import_batches
                SET status = 'awaiting_confirmation', updated_at = now()
                WHERE id = :batch_id AND admin_id = :admin_id AND status = 'collecting'
                  AND EXISTS (
                    SELECT 1 FROM women_help_certificate_import_items
                    WHERE batch_id = :batch_id AND status = 'ready'
                  )
            """), {"batch_id": batch_id, "admin_id": admin_id})
        return await self.summary(admin_id, batch_id)

    async def summary(self, admin_id: int, batch_id: int) -> BatchSummary | None:
        async with self._engine.connect() as connection:
            row = (await connection.execute(text("""
                SELECT b.id AS batch_id, b.pool_slug, b.status, b.target_count,
                    count(i.id) FILTER (WHERE i.status = 'ready')::int AS ready,
                    count(i.id) FILTER (WHERE i.status = 'duplicate')::int AS duplicates,
                    count(i.id) FILTER (WHERE i.status = 'invalid')::int AS invalid
                FROM women_help_certificate_import_batches b
                LEFT JOIN women_help_certificate_import_items i ON i.batch_id = b.id
                WHERE b.id = :batch_id AND b.admin_id = :admin_id
                GROUP BY b.id
            """), {"batch_id": batch_id, "admin_id": admin_id})).mappings().first()
        return BatchSummary(**row) if row is not None else None

    async def ready_documents(
        self, admin_id: int, batch_id: int
    ) -> tuple[tuple[ParsedCertificatePdf, CertificateObjectRef], ...]:
        async with self._engine.connect() as connection:
            batch = (await connection.execute(text("""
                SELECT pool_slug FROM women_help_certificate_import_batches
                WHERE id = :batch_id AND admin_id = :admin_id
                  AND status = 'awaiting_confirmation' AND expires_at > now()
            """), {"batch_id": batch_id, "admin_id": admin_id})).first()
            if batch is None:
                return ()
            rows = (await connection.execute(text("""
                SELECT * FROM women_help_certificate_import_items
                WHERE batch_id = :batch_id AND status = 'ready' ORDER BY id
            """), {"batch_id": batch_id})).mappings()
            provider_slug = batch.pool_slug
            provider = "Ozon" if provider_slug == "ozon" else "Пятёрочка"
            return tuple((
                ParsedCertificatePdf(
                    provider_slug=provider_slug, provider=provider,
                    nominal_rubles=row["nominal_rubles"],
                    activation_code=row["activation_code"], serial_number=row["serial_number"],
                    valid_from=row["valid_from"], expires_at=row["expires_at"],
                    is_test=row["is_test"], filename=row["pdf_filename"],
                    pdf_sha256=row["pdf_sha256"], pdf_bytes=b"",
                ),
                CertificateObjectRef(
                    bucket=row["pdf_bucket"], key=row["pdf_object_key"],
                    version_id=row["pdf_version_id"], sha256=row["pdf_sha256"],
                    size=row["pdf_size"],
                ),
            ) for row in rows)

    async def cancel_batch(
        self, admin_id: int, batch_id: int, *, expired: bool = False
    ) -> tuple[CertificateObjectRef, ...]:
        async with self._engine.begin() as connection:
            rows = (await connection.execute(text("""
                SELECT pdf_bucket, pdf_object_key, pdf_version_id, pdf_sha256, pdf_size
                FROM women_help_certificate_import_items i
                JOIN women_help_certificate_import_batches b ON b.id = i.batch_id
                WHERE b.id = :batch_id AND b.admin_id = :admin_id
                  AND b.status IN ('collecting','awaiting_confirmation')
                  AND i.status = 'ready'
            """), {"batch_id": batch_id, "admin_id": admin_id})).mappings()
            refs = tuple(CertificateObjectRef(
                bucket=row["pdf_bucket"], key=row["pdf_object_key"],
                version_id=row["pdf_version_id"], sha256=row["pdf_sha256"],
                size=row["pdf_size"],
            ) for row in rows)
            result = await connection.execute(text("""
                UPDATE women_help_certificate_import_batches
                SET status = :status, updated_at = now()
                WHERE id = :batch_id AND admin_id = :admin_id
                  AND status IN ('collecting','awaiting_confirmation')
            """), {
                "status": "expired" if expired else "cancelled",
                "batch_id": batch_id, "admin_id": admin_id,
            })
            if result.rowcount != 1:
                return ()
            await connection.execute(text("""
                UPDATE women_help_certificate_import_items
                SET activation_code = NULL, serial_number = NULL,
                    pdf_bucket = NULL, pdf_object_key = NULL, pdf_version_id = NULL
                WHERE batch_id = :batch_id
            """), {"batch_id": batch_id})
            return refs

    async def expired_batches(self) -> tuple[tuple[int, int], ...]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(text("""
                SELECT admin_id, id AS batch_id
                FROM women_help_certificate_import_batches
                WHERE status IN ('collecting','awaiting_confirmation')
                  AND expires_at <= now()
                ORDER BY id
            """))).mappings()
            return tuple((row["admin_id"], row["batch_id"]) for row in rows)

    async def close(self) -> None:
        await self._engine.dispose()

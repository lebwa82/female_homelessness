from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.domain import (
    DELIVERY_AMBIGUOUS_CATEGORY,
    ConversationState,
    EscalationRequest,
    InboundExecutionKey,
    RiskAssessment,
)
from app.pii import redact_with_audit

CALLBACK_PROCESSING_LEASE = timedelta(minutes=5)
_AUDIT_FIELDS = frozenset({
    "status",
    "diagnostic_status",
    "provider",
    "agent",
    "input_hash",
    "request",
    "pii_redaction",
    "latency_ms",
    "usage",
    "output_shape",
    "validation_errors",
    "normalization",
    "rationale_alias_used",
    "evidence",
    "error_type",
})
_VALIDATION_FIELDS = frozenset({
    "level",
    "categories",
    "confidence",
    "rationale",
    "rationale_alias_used",
    "evidence_claims",
    "intent",
    "need_hints",
    "draft_text",
    "suggested_support",
})
_VALIDATION_TYPES = frozenset({
    "missing",
    "extra_forbidden",
    "enum",
    "string_type",
    "string_too_short",
    "string_too_long",
    "float_type",
    "float_parsing",
    "greater_than_equal",
    "less_than_equal",
    "tuple_type",
    "list_type",
    "too_long",
    "bool_type",
    "bool_parsing",
    "model_type",
    "dict_type",
    "json_invalid",
})
_SAFE_ERROR_TYPES = frozenset({
    "TimeoutError",
    "ConnectionError",
    "OSError",
    "AuthenticationError",
    "PermissionDeniedError",
    "RateLimitError",
    "BadRequestError",
    "InternalServerError",
    "APIStatusError",
    "UnexpectedModelBehavior",
})


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("channel", "channel_user_id", name="uq_conversations_channel_identity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), default="telegram")
    channel_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    platform_user_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(48), default=ConversationState.GREETING.value)
    stage: Mapped[str] = mapped_column(String(32), default="welcome")
    requested_help: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_aid_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_contact_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pending_district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pending_offer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    context_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    human_handoff: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConversationTombstone(Base):
    """Minimal deletion barrier; it retains neither raw identity nor content."""

    __tablename__ = "conversation_tombstones"
    __table_args__ = (UniqueConstraint("channel", "platform_user_hash", name="uq_conversation_tombstones_identity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32))
    platform_user_hash: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text, default="")
    audit: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    redacted_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    context_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskAssessmentRecord(Base):
    __tablename__ = "risk_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    level: Mapped[str] = mapped_column(String(32), index=True)
    categories: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float] = mapped_column(default=0.0)
    detector: Mapped[str] = mapped_column(String(64))
    rationale: Mapped[str] = mapped_column(String(240), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    audit: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActionExecution(Base):
    __tablename__ = "action_executions"
    __table_args__ = (UniqueConstraint("effect_key", name="uq_action_executions_effect_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    effect_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    audit: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallbackExecution(Base):
    __tablename__ = "callback_executions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "callback_id",
            "source_message_id",
            name="uq_callback_executions_origin",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    callback_id: Mapped[str] = mapped_column(String(64))
    source_message_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InboundTextExecution(Base):
    __tablename__ = "inbound_text_executions"
    __table_args__ = (
        UniqueConstraint("conversation_id", "source_message_id", name="uq_inbound_text_executions_origin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    source_message_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    delivery_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    delivery_ambiguity_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AidRequest(Base):
    __tablename__ = "aid_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    aid_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="requested", index=True)
    request_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContactPoint(Base):
    __tablename__ = "contact_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    aid_request_id: Mapped[int] = mapped_column(ForeignKey("aid_requests.id"), index=True)
    method: Mapped[str] = mapped_column(String(64))
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Escalation(Base):
    __tablename__ = "escalations"
    __table_args__ = (UniqueConstraint("request_key", name="uq_escalations_request_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    cause: Mapped[str] = mapped_column(String(48), default="safety", index=True)
    level: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    categories: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reason: Mapped[str] = mapped_column(String(240), default="")
    request_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="simulated", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowupJob(Base):
    __tablename__ = "followup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    conversation_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    aid_request_id: Mapped[int | None] = mapped_column(ForeignKey("aid_requests.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


engine: AsyncEngine = create_async_engine(settings.database_url)
Session = async_sessionmaker(engine, expire_on_commit=False)
_BOUND_REPOSITORY_SESSION: ContextVar[AsyncSession | None] = ContextVar(
    "bound_repository_session",
    default=None,
)


@contextmanager
def bind_repository_session(session: AsyncSession) -> Iterator[None]:
    """Bind production repository functions to one caller-owned transaction."""
    token = _BOUND_REPOSITORY_SESSION.set(session)
    try:
        yield
    finally:
        _BOUND_REPOSITORY_SESSION.reset(token)


@asynccontextmanager
async def repository_session() -> AsyncIterator[AsyncSession]:
    bound = _BOUND_REPOSITORY_SESSION.get()
    if bound is not None:
        yield bound
        return
    async with Session() as session:
        yield session


async def finish_repository_write(session: AsyncSession) -> None:
    """Flush inside a bound UoW; otherwise preserve the standalone commit API."""
    if _BOUND_REPOSITORY_SESSION.get() is session:
        await session.flush()
    else:
        await session.commit()


def content_expiry_at(now: datetime | None = None, *, retention_days: int | None = None) -> datetime:
    """Return the one configurable retention deadline for messages and contacts."""
    return (now or datetime.now(UTC)) + timedelta(
        days=settings.message_retention_days if retention_days is None else retention_days
    )


def sanitize_agent_audit(audit: dict[str, Any]) -> dict[str, Any]:
    """Persist only finite, non-content diagnostics from an untrusted provider boundary."""
    result: dict[str, Any] = {}
    if not isinstance(audit, dict):
        return {"status": "unknown"}
    for field in sorted(_AUDIT_FIELDS.intersection(audit)):
        value = audit[field]
        if field in {"status", "diagnostic_status"}:
            result[field] = _audit_category(value, {"completed", "invalid", "unavailable", "error", "not_configured", "fixture"})
        elif field == "provider":
            result[field] = "yandex_ai_studio" if value == "yandex_ai_studio" else "other_provider"
        elif field == "agent":
            result[field] = _audit_category(value, {"risk", "support", "safety"})
        elif field == "input_hash" and isinstance(value, str) and len(value) == 64:
            result[field] = value
        elif field == "request" and isinstance(value, dict):
            result[field] = {
                key: value[key]
                for key in ("temperature", "max_tokens", "reasoning_effort", "data_logging_enabled")
                if isinstance(value.get(key), (str, int, float, bool))
            }
        elif field == "pii_redaction" and isinstance(value, dict):
            result[field] = _safe_pii_audit(value)
        elif field == "latency_ms" and isinstance(value, (int, float)):
            result[field] = max(0, min(int(value), 120_000))
        elif field == "usage" and isinstance(value, dict):
            result[field] = {
                key: max(0, min(int(value[key]), 1_000_000))
                for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens")
                if isinstance(value.get(key), int)
            }
        elif field == "output_shape" and isinstance(value, dict):
            result[field] = {
                key: value[key]
                for key in ("characters", "nonempty", "starts_json", "ends_object", "starts_code_fence", "ends_code_fence")
                if isinstance(value.get(key), (int, bool))
            }
        elif field == "validation_errors" and isinstance(value, dict):
            result[field] = {
                "fields": sorted({_validation_field_category(item) for item in value.get("fields", []) if isinstance(item, str)}),
                "types": sorted({_validation_type_category(item) for item in value.get("types", []) if isinstance(item, str)}),
            }
        elif field == "normalization" and isinstance(value, dict):
            categories = value.get("categories", [])
            result[field] = {
                "categories": sorted(
                    category
                    for category in categories
                    if category in {"safety_rationale_truncated", "support_unknown_intent_cleared", "support_unknown_need_hints_cleared"}
                )
            }
        elif field == "rationale_alias_used" and isinstance(value, bool):
            result[field] = value
        elif field == "evidence" and isinstance(value, dict):
            result[field] = {
                key: max(0, min(int(value[key]), 100))
                for key in ("claims", "valid", "invalid")
                if isinstance(value.get(key), int)
            }
        elif field == "error_type":
            result[field] = _audit_category(value, _SAFE_ERROR_TYPES, fallback="OtherTransportError")
    return result or {"status": "unknown"}


def _audit_category(value: Any, allowed: set[str] | frozenset[str], fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _validation_field_category(value: str) -> str:
    return value if value in _VALIDATION_FIELDS else "unknown_field"


def _validation_type_category(value: str) -> str:
    return value if value in _VALIDATION_TYPES else "other_validation_error"


def _safe_pii_audit(value: dict[str, Any]) -> dict[str, Any]:
    counts = value.get("entity_counts", {})
    allowed_entities = {"PERSON", "LOCATION", "PHONE_NUMBER", "EMAIL_ADDRESS", "CREDIT_CARD", "IP_ADDRESS", "URL", "TELEGRAM_HANDLE"}
    safe_counts = {
        (key if key in allowed_entities else "OTHER"): max(0, min(int(count), 10_000))
        for key, count in counts.items()
        if isinstance(key, str) and isinstance(count, int)
    } if isinstance(counts, dict) else {}
    return {
        "engine": "presidio" if value.get("engine") == "presidio" else "other",
        "language": "ru" if value.get("language") == "ru" else "other",
        "detected": bool(value.get("detected")),
        "entity_counts": safe_counts,
        "entities_total": max(0, min(int(value.get("entities_total", 0)), 10_000))
        if isinstance(value.get("entities_total", 0), int)
        else 0,
    }


def conversation_identity_hash(channel: str, platform_user_id: int, key: str) -> str:
    return hmac.new(key.encode(), f"{channel}:{platform_user_id}".encode(), hashlib.sha256).hexdigest()


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for statement in (
            "ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_channel_user_id_key",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_channel_identity ON conversations (channel, channel_user_id)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS platform_user_hash VARCHAR(64) NOT NULL DEFAULT ''",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS chat_id BIGINT",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS username VARCHAR(128)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS state VARCHAR(48) NOT NULL DEFAULT 'greeting'",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_aid_id VARCHAR(64)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_contact_method VARCHAR(64)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_city VARCHAR(120)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_district VARCHAR(120)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pending_offer VARCHAR(64)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS generation INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS context_epoch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0",
            """
            CREATE TABLE IF NOT EXISTS conversation_tombstones (
                id SERIAL PRIMARY KEY,
                channel VARCHAR(32) NOT NULL,
                platform_user_hash VARCHAR(64) NOT NULL,
                generation INTEGER NOT NULL,
                deleted_at TIMESTAMPTZ DEFAULT now()
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_tombstones_identity
            ON conversation_tombstones (channel, platform_user_hash)
            """,
            "ALTER TABLE action_executions ADD COLUMN IF NOT EXISTS effect_key VARCHAR(128)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_action_executions_effect_key ON action_executions (effect_key)",
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS redacted_content TEXT",
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS context_epoch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
            (
                "UPDATE conversation_messages SET expires_at = created_at + "
                f"make_interval(days => {settings.message_retention_days}) WHERE expires_at IS NULL"
            ),
            (
                "UPDATE contact_points SET expires_at = created_at + "
                f"make_interval(days => {settings.message_retention_days}) WHERE expires_at IS NULL"
            ),
            "ALTER TABLE followup_jobs ADD COLUMN IF NOT EXISTS conversation_generation INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE followup_jobs ADD COLUMN IF NOT EXISTS lease_token VARCHAR(64)",
            "ALTER TABLE followup_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
            (
                "UPDATE followup_jobs SET status = 'pending', lease_token = NULL "
                "WHERE status = 'processing' AND lease_expires_at IS NULL"
            ),
            "CREATE INDEX IF NOT EXISTS ix_followup_jobs_lease_expires_at ON followup_jobs (lease_expires_at)",
            "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS outcome JSONB",
            "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ",
            "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS delivery_token VARCHAR(64)",
            "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS delivery_lease_expires_at TIMESTAMPTZ",
            (
                "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS "
                "delivery_status VARCHAR(32) NOT NULL DEFAULT 'pending'"
            ),
            (
                "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS "
                "delivery_ambiguity_count INTEGER NOT NULL DEFAULT 0"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_inbound_text_executions_delivery_lease_expires_at "
                "ON inbound_text_executions (delivery_lease_expires_at)"
            ),
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE escalations ADD COLUMN IF NOT EXISTS cause VARCHAR(48) NOT NULL DEFAULT 'safety'",
            "ALTER TABLE escalations ALTER COLUMN level DROP NOT NULL",
            "ALTER TABLE escalations ADD COLUMN IF NOT EXISTS request_key VARCHAR(128)",
            "CREATE INDEX IF NOT EXISTS ix_escalations_cause ON escalations (cause)",
            "ALTER TABLE escalations DROP CONSTRAINT IF EXISTS escalations_request_key_key",
            "DROP INDEX IF EXISTS escalations_request_key_key",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_escalations_request_key "
                "ON escalations (request_key)"
            ),
            """
            CREATE TABLE IF NOT EXISTS callback_executions (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                callback_id VARCHAR(64) NOT NULL,
                source_message_id VARCHAR(32) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'processing',
                lease_token VARCHAR(64),
                lease_expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """,
            "ALTER TABLE callback_executions ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'completed'",
            "ALTER TABLE callback_executions ADD COLUMN IF NOT EXISTS lease_token VARCHAR(64)",
            "ALTER TABLE callback_executions ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
            "UPDATE callback_executions SET status = 'completed' WHERE status IS NULL",
            "CREATE INDEX IF NOT EXISTS ix_callback_executions_status ON callback_executions (status)",
            (
                "CREATE INDEX IF NOT EXISTS ix_callback_executions_lease_expires_at "
                "ON callback_executions (lease_expires_at)"
            ),
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_callback_executions_origin
            ON callback_executions (conversation_id, callback_id, source_message_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS inbound_text_executions (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                source_message_id VARCHAR(32) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'processing',
                lease_token VARCHAR(64),
                lease_expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """,
            "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'completed'",
            "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS lease_token VARCHAR(64)",
            "ALTER TABLE inbound_text_executions ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
            "UPDATE inbound_text_executions SET status = 'completed' WHERE status IS NULL",
            "CREATE INDEX IF NOT EXISTS ix_inbound_text_executions_status ON inbound_text_executions (status)",
            (
                "CREATE INDEX IF NOT EXISTS ix_inbound_text_executions_lease_expires_at "
                "ON inbound_text_executions (lease_expires_at)"
            ),
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_text_executions_origin
            ON inbound_text_executions (conversation_id, source_message_id)
            """,
        ):
            await connection.execute(text(statement))


async def get_or_create_conversation_record(
    channel: str,
    platform_user_id: int,
    chat_id: int,
    username: str | None,
    identity_hash_key: str,
) -> Conversation:
    identity_hash = conversation_identity_hash(channel, platform_user_id, identity_hash_key)
    async with repository_session() as session:
        result = await session.execute(
            select(Conversation).where(
                Conversation.channel == channel,
                Conversation.channel_user_id == platform_user_id,
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            tombstone_result = await session.execute(
                select(ConversationTombstone).where(
                    ConversationTombstone.channel == channel,
                    ConversationTombstone.platform_user_hash == identity_hash,
                )
            )
            tombstone = tombstone_result.scalar_one_or_none()
            created = await session.execute(
                postgres_insert(Conversation)
                .values(
                    channel=channel,
                    channel_user_id=platform_user_id,
                    chat_id=chat_id,
                    username=username,
                    platform_user_hash=identity_hash,
                    generation=tombstone.generation if tombstone is not None else 0,
                )
                .on_conflict_do_nothing(index_elements=(Conversation.channel, Conversation.channel_user_id))
                .returning(Conversation.id)
            )
            conversation_id = created.scalar_one_or_none()
            if conversation_id is None:
                concurrent = await session.execute(
                    select(Conversation).where(
                        Conversation.channel == channel,
                        Conversation.channel_user_id == platform_user_id,
                    )
                )
                conversation = concurrent.scalar_one()
                conversation.chat_id = chat_id
                conversation.username = username
            else:
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None:
                    raise LookupError("new conversation is missing")
        else:
            conversation.chat_id = chat_id
            conversation.username = username
        await finish_repository_write(session)
        await session.refresh(conversation)
        return conversation


async def append_message(
    conversation_id: int,
    role: str,
    content: str,
    audit: dict[str, Any] | None = None,
    retention_days: int | None = None,
    context_epoch: int = 0,
) -> ConversationMessage:
    redaction = redact_with_audit(content)
    async with repository_session() as session:
        message = ConversationMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            redacted_content=redaction.text,
            audit={"pii_redaction": redaction.audit, **(audit or {})},
            context_epoch=context_epoch,
            expires_at=content_expiry_at(retention_days=retention_days),
        )
        session.add(message)
        await finish_repository_write(session)
        await session.refresh(message)
        return message


async def load_history(conversation_id: int) -> list[tuple[str, str]]:
    now = datetime.now(UTC)
    async with repository_session() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.expires_at.is_not(None),
                ConversationMessage.expires_at > now,
            )
            .order_by(ConversationMessage.id)
        )
        return [(item.role, item.content) for item in result.scalars()]


async def load_model_history(conversation_id: int, context_epoch: int = 0) -> list[tuple[str, str]]:
    """Load only active pre-redacted history for provider-bound context."""
    now = datetime.now(UTC)
    async with repository_session() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.context_epoch == context_epoch,
                ConversationMessage.expires_at.is_not(None),
                ConversationMessage.expires_at > now,
            )
            .order_by(ConversationMessage.id)
        )
        return [
            (
                item.role,
                "[CONTACT]" if item.audit.get("content_type") == "contact_value" else (item.redacted_content or ""),
            )
            for item in result.scalars()
        ]


async def load_active_contact_points(aid_request_id: int) -> list[ContactPoint]:
    now = datetime.now(UTC)
    async with repository_session() as session:
        result = await session.execute(
            select(ContactPoint).where(
                ContactPoint.aid_request_id == aid_request_id,
                ContactPoint.expires_at.is_not(None),
                ContactPoint.expires_at > now,
            )
        )
        return list(result.scalars())


async def record_event(
    conversation_id: int, kind: str, payload: str = "", audit: dict[str, Any] | None = None
) -> None:
    async with repository_session() as session:
        session.add(Event(conversation_id=conversation_id, kind=kind, payload=payload[:1200], audit=audit or {}))
        await finish_repository_write(session)


async def record_agent_run(conversation_id: int, agent_name: str, audit: dict[str, Any]) -> None:
    safe_audit = sanitize_agent_audit(audit)
    async with repository_session() as session:
        session.add(
            AgentRun(
                conversation_id=conversation_id,
                agent_name=agent_name,
                status=str(safe_audit.get("status", "unknown")),
                input_hash=str(safe_audit.get("input_hash", "")),
                audit=safe_audit,
            )
        )
        await finish_repository_write(session)


async def record_risk_assessment(conversation_id: int, assessment: RiskAssessment) -> None:
    async with repository_session() as session:
        session.add(
            RiskAssessmentRecord(
                conversation_id=conversation_id,
                level=assessment.level.value,
                categories={"items": list(assessment.categories)},
                confidence=assessment.confidence,
                detector=assessment.detector,
                rationale=assessment.rationale,
            )
        )
        await finish_repository_write(session)


async def record_action(
    conversation_id: int,
    kind: str,
    status: str,
    audit: dict[str, Any] | None = None,
    effect_key: str | None = None,
) -> None:
    async with repository_session() as session:
        values = {
            "conversation_id": conversation_id,
            "kind": kind,
            "status": status,
            "audit": audit or {},
            "effect_key": effect_key,
        }
        if effect_key is None:
            session.add(ActionExecution(**values))
        else:
            await session.execute(
                postgres_insert(ActionExecution)
                .values(**values)
                .on_conflict_do_nothing(index_elements=(ActionExecution.effect_key,))
            )
        await finish_repository_write(session)


async def claim_callback_execution(
    conversation_id: int,
    callback_id: str,
    message_id: int | None,
) -> str | None:
    del callback_id
    source_message_id = str(message_id) if message_id is not None else "missing"
    callback_slot = "keyboard-slot"
    now = datetime.now(UTC)
    lease_token = uuid4().hex
    async with repository_session() as session:
        statement = (
            postgres_insert(CallbackExecution)
            .values(
                conversation_id=conversation_id,
                callback_id=callback_slot,
                source_message_id=source_message_id,
                status="processing",
                lease_token=lease_token,
                lease_expires_at=now + CALLBACK_PROCESSING_LEASE,
            )
            .on_conflict_do_update(
                index_elements=(
                    CallbackExecution.conversation_id,
                    CallbackExecution.callback_id,
                    CallbackExecution.source_message_id,
                ),
                set_={
                    "status": "processing",
                    "lease_token": lease_token,
                    "lease_expires_at": now + CALLBACK_PROCESSING_LEASE,
                },
                where=or_(
                    CallbackExecution.status == "failed",
                    and_(
                        CallbackExecution.status == "processing",
                        CallbackExecution.lease_expires_at <= now,
                    ),
                ),
            )
            .returning(CallbackExecution.lease_token)
        )
        result = await session.execute(statement)
        await finish_repository_write(session)
        return result.scalar_one_or_none()


async def complete_callback_execution(
    conversation_id: int,
    callback_id: str,
    message_id: int | None,
    lease_token: str,
) -> bool:
    del callback_id
    source_message_id = str(message_id) if message_id is not None else "missing"
    callback_slot = "keyboard-slot"
    async with repository_session() as session:
        result = await session.execute(
            update(CallbackExecution)
            .where(
                CallbackExecution.conversation_id == conversation_id,
                CallbackExecution.callback_id == callback_slot,
                CallbackExecution.source_message_id == source_message_id,
                CallbackExecution.status == "processing",
                CallbackExecution.lease_token == lease_token,
            )
            .values(status="completed", lease_token=None, lease_expires_at=None)
        )
        await finish_repository_write(session)
        return result.rowcount == 1


async def fail_callback_execution(
    conversation_id: int,
    callback_id: str,
    message_id: int | None,
    lease_token: str,
) -> bool:
    del callback_id
    source_message_id = str(message_id) if message_id is not None else "missing"
    callback_slot = "keyboard-slot"
    async with repository_session() as session:
        result = await session.execute(
            update(CallbackExecution)
            .where(
                CallbackExecution.conversation_id == conversation_id,
                CallbackExecution.callback_id == callback_slot,
                CallbackExecution.source_message_id == source_message_id,
                CallbackExecution.status == "processing",
                CallbackExecution.lease_token == lease_token,
            )
            .values(status="failed", lease_token=None, lease_expires_at=None)
        )
        await finish_repository_write(session)
        return result.rowcount == 1


def _execution_storage_key(message_id: InboundExecutionKey | int | None) -> str:
    if isinstance(message_id, InboundExecutionKey):
        return message_id.storage_key
    return InboundExecutionKey.message(message_id).storage_key


async def claim_text_execution(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
) -> str | None:
    source_message_id = _execution_storage_key(message_id)
    now = datetime.now(UTC)
    lease_token = uuid4().hex
    async with repository_session() as session:
        result = await session.execute(
            postgres_insert(InboundTextExecution)
            .values(
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                status="processing",
                lease_token=lease_token,
                lease_expires_at=now + CALLBACK_PROCESSING_LEASE,
            )
            .on_conflict_do_update(
                index_elements=(
                    InboundTextExecution.conversation_id,
                    InboundTextExecution.source_message_id,
                ),
                set_={
                    "status": "processing",
                    "lease_token": lease_token,
                    "lease_expires_at": now + CALLBACK_PROCESSING_LEASE,
                },
                where=or_(
                    InboundTextExecution.status == "failed",
                    and_(
                        InboundTextExecution.status == "processing",
                        InboundTextExecution.lease_expires_at <= now,
                    ),
                ),
            )
            .returning(InboundTextExecution.lease_token)
        )
        await finish_repository_write(session)
        return result.scalar_one_or_none()


async def complete_text_execution(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
    lease_token: str,
) -> bool:
    source_message_id = _execution_storage_key(message_id)
    async with repository_session() as session:
        result = await session.execute(
            update(InboundTextExecution)
            .where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.status == "processing",
                InboundTextExecution.lease_token == lease_token,
            )
            .values(status="completed", lease_token=None, lease_expires_at=None)
        )
        await finish_repository_write(session)
        return result.rowcount == 1


async def save_text_execution_outcome(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
    lease_token: str,
    outcome: dict[str, Any],
) -> bool:
    """Persist the deterministic rendered turn before the inbound claim completes."""
    source_message_id = _execution_storage_key(message_id)
    async with repository_session() as session:
        result = await session.execute(
            update(InboundTextExecution)
            .where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.status == "processing",
                InboundTextExecution.lease_token == lease_token,
            )
            .values(
                outcome=outcome,
                status="completed",
                lease_token=None,
                lease_expires_at=None,
            )
        )
        await finish_repository_write(session)
        return result.rowcount == 1


async def load_text_execution_outcome(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
) -> tuple[dict[str, Any], bool] | None:
    source_message_id = _execution_storage_key(message_id)
    async with repository_session() as session:
        result = await session.execute(
            select(InboundTextExecution.outcome, InboundTextExecution.delivered_at).where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.outcome.is_not(None),
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        outcome, delivered_at = row
        return outcome, delivered_at is not None


async def acknowledge_text_execution_outcome(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
) -> None:
    source_message_id = _execution_storage_key(message_id)
    async with repository_session() as session:
        await session.execute(
            update(InboundTextExecution)
            .where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.outcome.is_not(None),
                InboundTextExecution.delivered_at.is_(None),
            )
            .values(
                delivered_at=func.now(),
                delivery_token=None,
                delivery_lease_expires_at=None,
                delivery_status="acknowledged",
            )
        )
        await finish_repository_write(session)


async def mark_text_execution_delivery_ambiguous(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
) -> bool:
    """Persist the finite post-send/pre-ack transport observation."""
    source_message_id = _execution_storage_key(message_id)
    async with repository_session() as session:
        result = await session.execute(
            update(InboundTextExecution)
            .where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.outcome.is_not(None),
                InboundTextExecution.delivered_at.is_(None),
            )
            .values(
                delivery_status=DELIVERY_AMBIGUOUS_CATEGORY,
                delivery_ambiguity_count=InboundTextExecution.delivery_ambiguity_count + 1,
                delivery_token=None,
                delivery_lease_expires_at=None,
            )
        )
        await finish_repository_write(session)
        return result.rowcount == 1


async def claim_text_execution_delivery(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
) -> str | None:
    """Lease one durable outbox turn before handing it to Telegram."""
    source_message_id = _execution_storage_key(message_id)
    now = datetime.now(UTC)
    token = uuid4().hex
    async with repository_session() as session:
        result = await session.execute(
            update(InboundTextExecution)
            .where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.outcome.is_not(None),
                InboundTextExecution.delivered_at.is_(None),
                or_(
                    InboundTextExecution.delivery_token.is_(None),
                    InboundTextExecution.delivery_lease_expires_at.is_(None),
                    InboundTextExecution.delivery_lease_expires_at <= now,
                ),
            )
            .values(
                delivery_token=token,
                delivery_lease_expires_at=now + CALLBACK_PROCESSING_LEASE,
            )
            .returning(InboundTextExecution.delivery_token)
        )
        await finish_repository_write(session)
        return result.scalar_one_or_none()


async def release_text_execution_delivery(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
    delivery_token: str,
) -> bool:
    source_message_id = _execution_storage_key(message_id)
    async with repository_session() as session:
        result = await session.execute(
            update(InboundTextExecution)
            .where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.delivery_token == delivery_token,
                InboundTextExecution.delivered_at.is_(None),
            )
            .values(delivery_token=None, delivery_lease_expires_at=None)
        )
        await finish_repository_write(session)
        return result.rowcount == 1


async def fail_text_execution(
    conversation_id: int,
    message_id: InboundExecutionKey | int | None,
    lease_token: str,
) -> bool:
    source_message_id = _execution_storage_key(message_id)
    async with repository_session() as session:
        result = await session.execute(
            update(InboundTextExecution)
            .where(
                InboundTextExecution.conversation_id == conversation_id,
                InboundTextExecution.source_message_id == source_message_id,
                InboundTextExecution.status == "processing",
                InboundTextExecution.lease_token == lease_token,
            )
            .values(status="failed", lease_token=None, lease_expires_at=None)
        )
        await finish_repository_write(session)
        return result.rowcount == 1


async def create_escalation(conversation_id: int, request: EscalationRequest) -> Escalation:
    values = {
        "conversation_id": conversation_id,
        "cause": request.cause.value,
        "level": request.level.value if request.level else None,
        "categories": {"items": list(request.categories)},
        "reason": request.reason,
        "request_key": request.request_key,
    }
    async with repository_session() as session:
        if request.request_key is None:
            escalation = Escalation(**values)
            session.add(escalation)
        else:
            result = await session.execute(
                postgres_insert(Escalation)
                .values(**values)
                .on_conflict_do_nothing(index_elements=(Escalation.request_key,))
                .returning(Escalation.id)
            )
            escalation_id = result.scalar_one_or_none()
            if escalation_id is None:
                existing = await session.execute(
                    select(Escalation).where(Escalation.request_key == request.request_key)
                )
                escalation = existing.scalar_one()
            else:
                escalation = await session.get(Escalation, escalation_id)
                if escalation is None:
                    raise LookupError(f"escalation {escalation_id} is missing")
        await finish_repository_write(session)
        return escalation


async def purge_expired_content(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    async with repository_session() as session:
        messages = await session.execute(
            select(ConversationMessage).where(
                or_(ConversationMessage.expires_at.is_(None), ConversationMessage.expires_at <= now)
            )
        )
        contacts = await session.execute(
            select(ContactPoint).where(or_(ContactPoint.expires_at.is_(None), ContactPoint.expires_at <= now)))
        items = [*messages.scalars(), *contacts.scalars()]
        for item in items:
            await session.delete(item)
        await finish_repository_write(session)
        return len(items)


async def delete_conversation_data(conversation_id: int) -> None:
    """Irreversibly remove all identity-linked records in one transaction.

    No post-delete event is retained: the deletion confirmation is intentionally
    non-persistent so it cannot recreate a conversation identity.
    """
    async with repository_session() as session:
        get_conversation = getattr(session, "get", None)
        if get_conversation is not None:
            conversation = await get_conversation(Conversation, conversation_id, with_for_update=True)
            if conversation is None:
                return
            await session.execute(
                postgres_insert(ConversationTombstone)
                .values(
                    channel=conversation.channel,
                    platform_user_hash=conversation.platform_user_hash,
                    generation=conversation.generation + 1,
                )
                .on_conflict_do_update(
                    index_elements=(ConversationTombstone.channel, ConversationTombstone.platform_user_hash),
                    set_={"generation": func.greatest(ConversationTombstone.generation, conversation.generation + 1)},
                )
            )
        request_ids = select(AidRequest.id).where(AidRequest.conversation_id == conversation_id)
        await session.execute(delete(ContactPoint).where(ContactPoint.aid_request_id.in_(request_ids)))
        await session.execute(delete(FollowupJob).where(FollowupJob.conversation_id == conversation_id))
        await session.execute(delete(AidRequest).where(AidRequest.conversation_id == conversation_id))
        await session.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id))
        await session.execute(delete(CallbackExecution).where(CallbackExecution.conversation_id == conversation_id))
        await session.execute(delete(InboundTextExecution).where(InboundTextExecution.conversation_id == conversation_id))
        await session.execute(delete(AgentRun).where(AgentRun.conversation_id == conversation_id))
        await session.execute(delete(RiskAssessmentRecord).where(RiskAssessmentRecord.conversation_id == conversation_id))
        await session.execute(delete(ActionExecution).where(ActionExecution.conversation_id == conversation_id))
        await session.execute(delete(Escalation).where(Escalation.conversation_id == conversation_id))
        await session.execute(delete(Event).where(Event.conversation_id == conversation_id))
        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await finish_repository_write(session)


async def cancel_followup_reminders(conversation_id: int) -> None:
    async with repository_session() as session:
        await session.get(Conversation, conversation_id, with_for_update=True)
        await session.execute(
            delete(FollowupJob).where(
                FollowupJob.conversation_id == conversation_id,
                FollowupJob.status.in_(("pending", "processing")),
            )
        )
        await finish_repository_write(session)

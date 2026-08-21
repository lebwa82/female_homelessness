from __future__ import annotations

import hashlib
import hmac
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
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.domain import ConversationState, EscalationRequest, RiskAssessment
from app.pii import redact_with_audit

CALLBACK_PROCESSING_LEASE = timedelta(minutes=5)


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
    human_handoff: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    cause: Mapped[str] = mapped_column(String(48), default="safety", index=True)
    level: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    categories: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reason: Mapped[str] = mapped_column(String(240), default="")
    status: Mapped[str] = mapped_column(String(32), default="simulated", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowupJob(Base):
    __tablename__ = "followup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    aid_request_id: Mapped[int | None] = mapped_column(ForeignKey("aid_requests.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


engine: AsyncEngine = create_async_engine(settings.database_url)
Session = async_sessionmaker(engine, expire_on_commit=False)


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
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS redacted_content TEXT",
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE escalations ADD COLUMN IF NOT EXISTS cause VARCHAR(48) NOT NULL DEFAULT 'safety'",
            "ALTER TABLE escalations ALTER COLUMN level DROP NOT NULL",
            "CREATE INDEX IF NOT EXISTS ix_escalations_cause ON escalations (cause)",
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
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_callback_executions_origin
            ON callback_executions (conversation_id, callback_id, source_message_id)
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
    async with Session() as session:
        result = await session.execute(
            select(Conversation).where(
                Conversation.channel == channel,
                Conversation.channel_user_id == platform_user_id,
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            conversation = Conversation(
                channel=channel,
                channel_user_id=platform_user_id,
                chat_id=chat_id,
                username=username,
                platform_user_hash=conversation_identity_hash(channel, platform_user_id, identity_hash_key),
            )
            session.add(conversation)
        else:
            conversation.chat_id = chat_id
            conversation.username = username
        await session.commit()
        await session.refresh(conversation)
        return conversation


async def append_message(
    conversation_id: int,
    role: str,
    content: str,
    audit: dict[str, Any] | None = None,
    retention_days: int = 30,
) -> ConversationMessage:
    redaction = redact_with_audit(content)
    async with Session() as session:
        message = ConversationMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            redacted_content=redaction.text,
            audit={"pii_redaction": redaction.audit, **(audit or {})},
            expires_at=datetime.now(UTC) + timedelta(days=retention_days),
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        return message


async def load_history(conversation_id: int) -> list[tuple[str, str]]:
    async with Session() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.id)
        )
        return [(item.role, item.content) for item in result.scalars()]


async def record_event(
    conversation_id: int, kind: str, payload: str = "", audit: dict[str, Any] | None = None
) -> None:
    async with Session() as session:
        session.add(Event(conversation_id=conversation_id, kind=kind, payload=payload[:1200], audit=audit or {}))
        await session.commit()


async def record_agent_run(conversation_id: int, agent_name: str, audit: dict[str, Any]) -> None:
    async with Session() as session:
        session.add(
            AgentRun(
                conversation_id=conversation_id,
                agent_name=agent_name,
                status=str(audit.get("status", "unknown")),
                response_id=audit.get("response_id"),
                model=audit.get("model"),
                input_hash=str(audit.get("input_hash", "")),
                audit=audit,
            )
        )
        await session.commit()


async def record_risk_assessment(conversation_id: int, assessment: RiskAssessment) -> None:
    async with Session() as session:
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
        await session.commit()


async def record_action(
    conversation_id: int, kind: str, status: str, audit: dict[str, Any] | None = None
) -> None:
    async with Session() as session:
        session.add(ActionExecution(conversation_id=conversation_id, kind=kind, status=status, audit=audit or {}))
        await session.commit()


async def claim_callback_execution(
    conversation_id: int,
    callback_id: str,
    message_id: int | None,
) -> str | None:
    source_message_id = str(message_id) if message_id is not None else "missing"
    now = datetime.now(UTC)
    lease_token = uuid4().hex
    async with Session() as session:
        statement = (
            postgres_insert(CallbackExecution)
            .values(
                conversation_id=conversation_id,
                callback_id=callback_id,
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
        await session.commit()
        return result.scalar_one_or_none()


async def complete_callback_execution(
    conversation_id: int,
    callback_id: str,
    message_id: int | None,
    lease_token: str,
) -> bool:
    source_message_id = str(message_id) if message_id is not None else "missing"
    async with Session() as session:
        result = await session.execute(
            update(CallbackExecution)
            .where(
                CallbackExecution.conversation_id == conversation_id,
                CallbackExecution.callback_id == callback_id,
                CallbackExecution.source_message_id == source_message_id,
                CallbackExecution.status == "processing",
                CallbackExecution.lease_token == lease_token,
            )
            .values(status="completed", lease_token=None, lease_expires_at=None)
        )
        await session.commit()
        return result.rowcount == 1


async def fail_callback_execution(
    conversation_id: int,
    callback_id: str,
    message_id: int | None,
    lease_token: str,
) -> bool:
    source_message_id = str(message_id) if message_id is not None else "missing"
    async with Session() as session:
        result = await session.execute(
            update(CallbackExecution)
            .where(
                CallbackExecution.conversation_id == conversation_id,
                CallbackExecution.callback_id == callback_id,
                CallbackExecution.source_message_id == source_message_id,
                CallbackExecution.status == "processing",
                CallbackExecution.lease_token == lease_token,
            )
            .values(status="failed", lease_token=None, lease_expires_at=None)
        )
        await session.commit()
        return result.rowcount == 1


async def create_escalation(conversation_id: int, request: EscalationRequest) -> None:
    async with Session() as session:
        session.add(
            Escalation(
                conversation_id=conversation_id,
                cause=request.cause.value,
                level=request.level.value if request.level else None,
                categories={"items": list(request.categories)},
                reason=request.reason,
            )
        )
        await session.commit()


async def purge_expired_content(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    async with Session() as session:
        messages = await session.execute(
            select(ConversationMessage).where(ConversationMessage.expires_at.is_not(None), ConversationMessage.expires_at <= now)
        )
        contacts = await session.execute(
            select(ContactPoint).where(ContactPoint.expires_at.is_not(None), ContactPoint.expires_at <= now)
        )
        items = [*messages.scalars(), *contacts.scalars()]
        for item in items:
            await session.delete(item)
        await session.commit()
        return len(items)


async def delete_conversation_data(conversation_id: int) -> None:
    async with Session() as session:
        request_ids = select(AidRequest.id).where(AidRequest.conversation_id == conversation_id)
        await session.execute(delete(ContactPoint).where(ContactPoint.aid_request_id.in_(request_ids)))
        await session.execute(delete(FollowupJob).where(FollowupJob.conversation_id == conversation_id))
        await session.execute(delete(AidRequest).where(AidRequest.conversation_id == conversation_id))
        await session.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id))
        await session.execute(delete(CallbackExecution).where(CallbackExecution.conversation_id == conversation_id))
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.state = ConversationState.GREETING.value
            conversation.requested_help = None
            conversation.pending_aid_id = None
            conversation.pending_contact_method = None
            conversation.pending_city = None
            conversation.pending_district = None
            conversation.pending_offer = None
        session.add(Event(conversation_id=conversation_id, kind="data_deleted", payload="", audit={}))
        await session.commit()


async def cancel_followup_reminders(conversation_id: int) -> None:
    async with Session() as session:
        await session.execute(
            delete(FollowupJob).where(
                FollowupJob.conversation_id == conversation_id,
                FollowupJob.kind == "followup_reminder",
                FollowupJob.status == "pending",
            )
        )
        await session.commit()

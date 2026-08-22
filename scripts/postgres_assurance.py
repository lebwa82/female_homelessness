"""Verify the production PostgreSQL contract without committing assurance data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, text, update

from app import db

_REQUIRED_COLUMNS = {
    ("conversations", "pending_offer"), ("conversations", "generation"), ("conversations", "version"),
    ("conversation_tombstones", "platform_user_hash"), ("conversation_tombstones", "generation"),
    ("escalations", "cause"), ("escalations", "level"), ("escalations", "request_key"),
    ("callback_executions", "status"), ("callback_executions", "lease_token"),
    ("callback_executions", "lease_expires_at"), ("inbound_text_executions", "source_message_id"),
    ("inbound_text_executions", "status"), ("inbound_text_executions", "lease_token"),
    ("inbound_text_executions", "lease_expires_at"), ("inbound_text_executions", "outcome"),
    ("inbound_text_executions", "delivered_at"), ("inbound_text_executions", "delivery_token"),
    ("inbound_text_executions", "delivery_lease_expires_at"), ("action_executions", "effect_key"),
    ("conversation_messages", "expires_at"), ("contact_points", "expires_at"),
    ("followup_jobs", "conversation_generation"), ("followup_jobs", "lease_token"),
    ("followup_jobs", "lease_expires_at"),
}


@dataclass(frozen=True)
class IndexExpectation:
    table: str
    columns: tuple[str, ...]
    unique: bool = False
    predicate: str | None = None


# A name match is insufficient: columns, uniqueness and partial-index predicate
# are part of the database contract as well.
expected_indexes = {
    "uq_conversations_channel_identity": IndexExpectation("conversations", ("channel", "channel_user_id"), True),
    "uq_conversation_tombstones_identity": IndexExpectation("conversation_tombstones", ("channel", "platform_user_hash"), True),
    "uq_escalations_request_key": IndexExpectation("escalations", ("request_key",), True),
    "ix_escalations_cause": IndexExpectation("escalations", ("cause",)),
    "uq_callback_executions_origin": IndexExpectation("callback_executions", ("conversation_id", "callback_id", "source_message_id"), True),
    "ix_callback_executions_status": IndexExpectation("callback_executions", ("status",)),
    "ix_callback_executions_lease_expires_at": IndexExpectation("callback_executions", ("lease_expires_at",)),
    "uq_inbound_text_executions_origin": IndexExpectation("inbound_text_executions", ("conversation_id", "source_message_id"), True),
    "ix_inbound_text_executions_status": IndexExpectation("inbound_text_executions", ("status",)),
    "ix_inbound_text_executions_lease_expires_at": IndexExpectation("inbound_text_executions", ("lease_expires_at",)),
    "ix_inbound_text_executions_delivery_lease_expires_at": IndexExpectation("inbound_text_executions", ("delivery_lease_expires_at",)),
    "uq_action_executions_effect_key": IndexExpectation("action_executions", ("effect_key",), True),
    "ix_followup_jobs_lease_expires_at": IndexExpectation("followup_jobs", ("lease_expires_at",)),
}
_REQUIRED_INDEXES = frozenset(expected_indexes)


def _identifier(value: str) -> str:
    return value.strip().strip('"').split(".")[-1].strip('"').casefold()


def _predicate(value: str | None) -> str | None:
    return None if value is None else (re.sub(r"\s+", " ", value.strip().rstrip(";")).casefold() or None)


def _index_projection(indexdef: str) -> IndexExpectation | None:
    compact = re.sub(r"\s+", " ", indexdef.strip())
    match = re.search(
        r"^CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+.+?\s+ON\s+(?:[^. ]+\.)?(?P<table>[^ ]+)"
        r"(?:\s+USING\s+[^ ]+)?\s*\((?P<columns>[^)]+)\)(?:\s+WHERE\s+(?P<predicate>.+))?$",
        compact,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return IndexExpectation(
        table=_identifier(match.group("table")),
        columns=tuple(_identifier(column) for column in match.group("columns").split(",")),
        unique=match.group("unique") is not None,
        predicate=_predicate(match.group("predicate")),
    )


class RollbackRepository:
    """Exercise application table paths only through the caller's rollback connection."""

    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def seed_conversation(self) -> int:
        token = uuid4().hex
        result = await self.connection.execute(
            db.postgres_insert(db.Conversation)
            .values(
                channel="assurance",
                channel_user_id=int(token[:15], 16),
                platform_user_hash=hashlib.sha256(token.encode()).hexdigest(),
            )
            .returning(db.Conversation.id)
        )
        return result.scalar_one()

    async def claim_reclaim_and_ack_outbox(self, conversation_id: int) -> None:
        """Run claim → fail/reclaim → outcome → delivery acknowledgement on real models."""
        source_id, first, second = f"assurance:{uuid4().hex}", uuid4().hex, uuid4().hex
        now = datetime.now(UTC)
        await self.connection.execute(
            db.postgres_insert(db.InboundTextExecution).values(
                conversation_id=conversation_id, source_message_id=source_id, status="processing",
                lease_token=first, lease_expires_at=now + timedelta(minutes=5),
            )
        )
        for where, values in (
            ((db.InboundTextExecution.lease_token == first,), {"status": "failed", "lease_token": None, "lease_expires_at": None}),
            ((db.InboundTextExecution.status == "failed",), {"status": "processing", "lease_token": second, "lease_expires_at": now + timedelta(minutes=5)}),
            ((db.InboundTextExecution.lease_token == second,), {"status": "completed", "lease_token": None, "outcome": {"text": "assurance", "choices": []}}),
        ):
            await self.connection.execute(
                update(db.InboundTextExecution)
                .where(db.InboundTextExecution.conversation_id == conversation_id, db.InboundTextExecution.source_message_id == source_id, *where)
                .values(**values)
            )
        outcome = await self.connection.execute(
            select(db.InboundTextExecution.outcome).where(
                db.InboundTextExecution.conversation_id == conversation_id,
                db.InboundTextExecution.source_message_id == source_id,
            )
        )
        if outcome.scalar_one() != {"text": "assurance", "choices": []}:
            raise RuntimeError("outbox_outcome_unreadable")
        await self.connection.execute(
            update(db.InboundTextExecution)
            .where(db.InboundTextExecution.conversation_id == conversation_id, db.InboundTextExecution.source_message_id == source_id)
            .values(delivered_at=now)
        )

    async def retention_purge_and_read(self, conversation_id: int) -> None:
        await self.connection.execute(
            db.postgres_insert(db.ConversationMessage).values(
                conversation_id=conversation_id, role="assistant", content="assurance",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await self.connection.execute(
            delete(db.ConversationMessage).where(
                db.ConversationMessage.conversation_id == conversation_id,
                db.ConversationMessage.expires_at <= datetime.now(UTC),
            )
        )
        unreadable = await self.connection.execute(
            select(db.ConversationMessage.id).where(db.ConversationMessage.conversation_id == conversation_id)
        )
        if unreadable.scalar_one_or_none() is not None:
            raise RuntimeError("retention_purge_unreadable")

    async def delete_everything(self, conversation_id: int) -> None:
        """Use the exact production dependency set under rollback, not ad-hoc fixture SQL."""
        request_ids = select(db.AidRequest.id).where(db.AidRequest.conversation_id == conversation_id)
        statements = (
            delete(db.ContactPoint).where(db.ContactPoint.aid_request_id.in_(request_ids)),
            delete(db.FollowupJob).where(db.FollowupJob.conversation_id == conversation_id),
            delete(db.AidRequest).where(db.AidRequest.conversation_id == conversation_id),
            delete(db.ConversationMessage).where(db.ConversationMessage.conversation_id == conversation_id),
            delete(db.CallbackExecution).where(db.CallbackExecution.conversation_id == conversation_id),
            delete(db.InboundTextExecution).where(db.InboundTextExecution.conversation_id == conversation_id),
            delete(db.AgentRun).where(db.AgentRun.conversation_id == conversation_id),
            delete(db.RiskAssessmentRecord).where(db.RiskAssessmentRecord.conversation_id == conversation_id),
            delete(db.ActionExecution).where(db.ActionExecution.conversation_id == conversation_id),
            delete(db.Escalation).where(db.Escalation.conversation_id == conversation_id),
            delete(db.Event).where(db.Event.conversation_id == conversation_id),
            delete(db.Conversation).where(db.Conversation.id == conversation_id),
        )
        for statement in statements:
            await self.connection.execute(statement)


async def assure() -> dict[str, object]:
    """Run migrations twice, verify index definitions, then exercise rollback-only paths."""
    await db.init_db()
    await db.init_db()
    async with db.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            columns_result = await connection.execute(
                text("SELECT table_name, column_name, is_nullable FROM information_schema.columns WHERE table_schema = current_schema()")
            )
            columns = {(row.table_name, row.column_name): row.is_nullable for row in columns_result}
            indexes_result = await connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")
            )
            index_definitions = {row.indexname: str(getattr(row, "indexdef", "")) for row in indexes_result}
            missing_columns = _REQUIRED_COLUMNS - set(columns)
            missing_indexes = _REQUIRED_INDEXES - set(index_definitions)
            wrong_index_definition = {
                name for name, expected in expected_indexes.items()
                if name in index_definitions and _index_projection(index_definitions[name]) != expected
            }
            if missing_columns or missing_indexes or wrong_index_definition:
                raise RuntimeError("schema_assurance_failed")
            repository = RollbackRepository(connection)
            conversation_id = await repository.seed_conversation()
            await repository.claim_reclaim_and_ack_outbox(conversation_id)
            await repository.retention_purge_and_read(conversation_id)
            await repository.delete_everything(conversation_id)
        finally:
            await transaction.rollback()
    return {
        "init_runs": 2, "required_columns": len(_REQUIRED_COLUMNS), "required_indexes": len(_REQUIRED_INDEXES),
        "claim_reclaim_outcome": True, "retention_purge_read": True, "comprehensive_delete": True,
    }


async def main() -> int:
    try:
        result = await assure()
    except Exception as error:  # noqa: BLE001 - never print connection data
        print(f"postgres_assurance:failed:{type(error).__name__}")
        return 1
    print(json.dumps({"postgres_assurance": result}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

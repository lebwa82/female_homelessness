"""Verify the production PostgreSQL contract without committing assurance data."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.store import ConversationRecord, PostgresConversationStore
from app.worker import PostgresJobRepository

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
    method: str = "btree"


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
        r"(?:\s+USING\s+(?P<method>[^ ]+))?\s*\((?P<columns>[^)]+)\)(?:\s+WHERE\s+(?P<predicate>.+))?$",
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
        method=_identifier(match.group("method") or "btree"),
    )


async def _seed_legacy_followup_and_null_retention(
    conversation: db.Conversation,
    token: str,
    now: datetime,
) -> int:
    """Create through production code, then shape legacy nullable fields."""
    record = ConversationRecord(
        id=conversation.id,
        channel=conversation.channel,
        platform_user_id=conversation.channel_user_id,
        chat_id=conversation.chat_id or conversation.channel_user_id,
        username=conversation.username,
        generation=conversation.generation,
        version=conversation.version,
    )
    request = await PostgresConversationStore(token).create_aid_request(
        record,
        "legal_consultation",
        "email",
        "assurance@example.invalid",
        request_key=f"assurance:{token}",
    )
    message = await db.append_message(conversation.id, "assistant", "assurance", retention_days=1)
    async with db.repository_session() as session:
        followup = (
            await session.execute(
                select(db.FollowupJob).where(db.FollowupJob.aid_request_id == request.id)
            )
        ).scalar_one()
        contact = (
            await session.execute(
                select(db.ContactPoint).where(db.ContactPoint.aid_request_id == request.id)
            )
        ).scalar_one()
        stored_message = await session.get(db.ConversationMessage, message.id)
        if stored_message is None:
            raise RuntimeError("retention_fixture_missing")
        # These are the exact pre-migration shapes the production queries must
        # reclaim/purge; setup is direct so the exercised operations are not.
        followup.due_at = now - timedelta(seconds=1)
        followup.status = "processing"
        followup.lease_token = None
        followup.lease_expires_at = None
        contact.expires_at = None
        stored_message.expires_at = None
        await db.finish_repository_write(session)
    return request.id


async def _assert_delete_tombstone(conversation: db.Conversation) -> None:
    async with db.repository_session() as session:
        remaining = await session.execute(
            select(db.Conversation.id).where(db.Conversation.id == conversation.id)
        )
        tombstone = await session.execute(
            select(db.ConversationTombstone.generation).where(
                db.ConversationTombstone.channel == conversation.channel,
                db.ConversationTombstone.platform_user_hash == conversation.platform_user_hash,
            )
        )
        if remaining.scalar_one_or_none() is not None:
            raise RuntimeError("comprehensive_delete_failed")
        if tombstone.scalar_one_or_none() != conversation.generation + 1:
            raise RuntimeError("delete_tombstone_failed")


async def _exercise_production_repository() -> None:
    """Call the exact application repository functions bound to rollback."""
    token = uuid4().hex
    platform_user_id = int(token[:15], 16)
    message_id = int(token[15:27], 16)
    conversation = await db.get_or_create_conversation_record(
        "assurance",
        platform_user_id,
        platform_user_id,
        None,
        token,
    )
    lease = await db.claim_text_execution(conversation.id, message_id)
    if lease is None or not await db.fail_text_execution(conversation.id, message_id, lease):
        raise RuntimeError("outbox_initial_claim_failed")
    reclaimed = await db.claim_text_execution(conversation.id, message_id)
    if reclaimed is None:
        raise RuntimeError("outbox_reclaim_failed")
    payload = {
        "text": "assurance",
        "choices": [],
        "critical_delivery": False,
        "conversation_generation": conversation.generation,
        "inbound_execution_kind": "message",
    }
    if not await db.save_text_execution_outcome(conversation.id, message_id, reclaimed, payload):
        raise RuntimeError("outbox_outcome_failed")
    stored = await db.load_text_execution_outcome(conversation.id, message_id)
    if stored is None or stored[0] != payload:
        raise RuntimeError("outbox_outcome_unreadable")
    delivery_token = await db.claim_text_execution_delivery(conversation.id, message_id)
    if delivery_token is None:
        raise RuntimeError("outbox_delivery_claim_failed")
    await db.acknowledge_text_execution_outcome(conversation.id, message_id)

    now = datetime.now(UTC)
    request_id = await _seed_legacy_followup_and_null_retention(
        conversation,
        token,
        now,
    )
    jobs = await PostgresJobRepository().claim_due_jobs(now)
    if len(jobs) != 1 or jobs[0].lease_token is None:
        raise RuntimeError("followup_null_lease_reclaim_failed")
    first_job = jobs[0]
    await PostgresJobRepository().complete_job(first_job, False)
    reclaimed_jobs = await PostgresJobRepository().claim_due_jobs(now)
    if (
        len(reclaimed_jobs) != 1
        or reclaimed_jobs[0].id != first_job.id
        or reclaimed_jobs[0].lease_token in {None, first_job.lease_token}
    ):
        raise RuntimeError("followup_error_reclaim_failed")
    await PostgresJobRepository().discard_job(reclaimed_jobs[0])

    purged = await db.purge_expired_content(now)
    if purged < 2:
        raise RuntimeError("null_retention_purge_failed")
    if await db.load_history(conversation.id):
        raise RuntimeError("retention_purge_unreadable")
    if await db.load_active_contact_points(request_id):
        raise RuntimeError("contact_retention_purge_unreadable")
    await db.delete_conversation_data(conversation.id)
    await _assert_delete_tombstone(conversation)


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
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                with db.bind_repository_session(session):
                    await _exercise_production_repository()
            finally:
                await session.close()
        finally:
            await transaction.rollback()
    return {
        "init_runs": 2, "required_columns": len(_REQUIRED_COLUMNS), "required_indexes": len(_REQUIRED_INDEXES),
        "claim_reclaim_outcome": True, "followup_claim_reclaim": True,
        "null_retention_purge": True, "retention_purge_read": True,
        "comprehensive_delete": True, "delete_tombstone": True,
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

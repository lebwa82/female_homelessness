"""Safely verify the additive PostgreSQL schema contract against an existing database."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

from sqlalchemy import text

from app import db

_REQUIRED_COLUMNS = {
    ("conversations", "pending_offer"),
    ("conversations", "generation"),
    ("conversations", "version"),
    ("escalations", "cause"),
    ("escalations", "level"),
    ("escalations", "request_key"),
    ("callback_executions", "status"),
    ("callback_executions", "lease_token"),
    ("callback_executions", "lease_expires_at"),
    ("inbound_text_executions", "source_message_id"),
    ("inbound_text_executions", "status"),
    ("inbound_text_executions", "lease_token"),
    ("inbound_text_executions", "lease_expires_at"),
    ("inbound_text_executions", "outcome"),
    ("inbound_text_executions", "delivered_at"),
    ("action_executions", "effect_key"),
    ("conversation_messages", "expires_at"),
    ("contact_points", "expires_at"),
    ("followup_jobs", "conversation_generation"),
}
_REQUIRED_UNIQUE_INDEXES = frozenset(
    {
        "uq_escalations_request_key",
        "uq_callback_executions_origin",
        "uq_inbound_text_executions_origin",
        "uq_action_executions_effect_key",
    }
)
_REQUIRED_INDEXES = {
    "ix_escalations_cause",
    "ix_callback_executions_lease_expires_at",
    "ix_callback_executions_status",
    "uq_escalations_request_key",
    "uq_callback_executions_origin",
    "ix_inbound_text_executions_status",
    "ix_inbound_text_executions_lease_expires_at",
    "uq_inbound_text_executions_origin",
    "uq_action_executions_effect_key",
}


async def assure() -> dict[str, object]:
    """Run idempotent DDL twice, then inspect and verify compatibility in a rollback-only transaction."""
    await db.init_db()
    await db.init_db()
    async with db.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            columns_result = await connection.execute(
                text(
                    "SELECT table_name, column_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema()"
                )
            )
            columns = {(row.table_name, row.column_name): row.is_nullable for row in columns_result}
            indexes_result = await connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")
            )
            index_definitions = {
                row.indexname: str(getattr(row, "indexdef", "")) for row in indexes_result
            }
            token = uuid4().hex
            conversation_result = await connection.execute(
                text(
                    "INSERT INTO conversations (channel, channel_user_id, platform_user_hash) "
                    "VALUES ('assurance', :platform_user_id, :platform_user_hash) RETURNING id"
                ),
                {
                    "platform_user_id": int(token[:15], 16),
                    "platform_user_hash": hashlib.sha256(token.encode()).hexdigest(),
                },
            )
            conversation_id = conversation_result.scalar_one()
            request_key = f"assurance:{token}"
            await connection.execute(
                text(
                    "INSERT INTO escalations "
                    "(conversation_id, cause, level, categories, reason, request_key, status) "
                    "VALUES (:conversation_id, 'safety', 'human_requested', '{}'::jsonb, "
                    "'assurance', :request_key, 'simulated')"
                ),
                {"conversation_id": conversation_id, "request_key": request_key},
            )
            historical_level = (
                await connection.execute(
                    text("SELECT level FROM escalations WHERE request_key = :request_key"),
                    {"request_key": request_key},
                )
            ).scalar_one()
            if historical_level != "human_requested":
                raise RuntimeError("historical_level_unreadable")
            await connection.execute(
                text(
                    "INSERT INTO inbound_text_executions "
                    "(conversation_id, source_message_id, status, lease_token) "
                    "VALUES (:conversation_id, :source_message_id, 'processing', :lease_token)"
                ),
                {
                    "conversation_id": conversation_id,
                    "source_message_id": f"assurance:{token}",
                    "lease_token": token,
                },
            )
            claim_status = (
                await connection.execute(
                    text(
                        "SELECT status FROM inbound_text_executions "
                        "WHERE conversation_id = :conversation_id AND lease_token = :lease_token"
                    ),
                    {"conversation_id": conversation_id, "lease_token": token},
                )
            ).scalar_one()
            if claim_status != "processing":
                raise RuntimeError("claim_runtime_unreadable")
            await connection.execute(
                text(
                    "INSERT INTO conversation_messages (conversation_id, role, content, expires_at) "
                    "VALUES (:conversation_id, 'assistant', 'assurance', now() - interval '1 second')"
                ),
                {"conversation_id": conversation_id},
            )
            expired_messages = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM conversation_messages "
                        "WHERE conversation_id = :conversation_id AND expires_at <= now()"
                    ),
                    {"conversation_id": conversation_id},
                )
            ).scalar_one()
            if expired_messages != 1:
                raise RuntimeError("retention_runtime_unreadable")
            await connection.execute(
                text("DELETE FROM conversation_messages WHERE conversation_id = :conversation_id"),
                {"conversation_id": conversation_id},
            )
            await connection.execute(
                text("DELETE FROM inbound_text_executions WHERE conversation_id = :conversation_id"),
                {"conversation_id": conversation_id},
            )
            await connection.execute(
                text("DELETE FROM escalations WHERE conversation_id = :conversation_id"),
                {"conversation_id": conversation_id},
            )
            await connection.execute(
                text("DELETE FROM conversations WHERE id = :conversation_id"),
                {"conversation_id": conversation_id},
            )
        finally:
            await transaction.rollback()
    missing_columns = _REQUIRED_COLUMNS - set(columns)
    missing_indexes = _REQUIRED_INDEXES - set(index_definitions)
    malformed_unique_indexes = {
        name
        for name in _REQUIRED_UNIQUE_INDEXES
        if "CREATE UNIQUE INDEX" not in index_definitions.get(name, "").upper()
    }
    if (
        missing_columns
        or missing_indexes
        or malformed_unique_indexes
        or columns.get(("escalations", "level")) != "YES"
    ):
        raise RuntimeError("schema_assurance_failed")
    return {
        "init_runs": 2,
        "required_columns": len(_REQUIRED_COLUMNS),
        "required_indexes": len(_REQUIRED_INDEXES),
        "required_unique_indexes": len(_REQUIRED_UNIQUE_INDEXES),
        "historical_level_readable": True,
        "escalation_level_nullable": True,
        "claim_runtime_readable": True,
        "retention_runtime_readable": True,
        "delete_runtime_readable": True,
    }


async def main() -> int:
    try:
        result = await assure()
    except Exception as error:  # noqa: BLE001 - omit provider/connection details from console output
        print(f"postgres_assurance:failed:{type(error).__name__}")
        return 1
    print(json.dumps({"postgres_assurance": result}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

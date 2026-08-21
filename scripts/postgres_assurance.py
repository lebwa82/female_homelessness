"""Safely verify the additive PostgreSQL schema contract against an existing database."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app import db

_REQUIRED_COLUMNS = {
    ("conversations", "pending_offer"),
    ("escalations", "cause"),
    ("escalations", "level"),
    ("escalations", "request_key"),
    ("callback_executions", "status"),
    ("callback_executions", "lease_token"),
    ("callback_executions", "lease_expires_at"),
}
_REQUIRED_INDEXES = {
    "ix_escalations_cause",
    "ix_callback_executions_lease_expires_at",
    "ix_callback_executions_status",
    "uq_escalations_request_key",
    "uq_callback_executions_origin",
}


async def assure() -> dict[str, object]:
    """Run idempotent DDL twice, then inspect metadata in a rolled-back read transaction."""
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
                text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
            )
            indexes = {row.indexname for row in indexes_result}
            await connection.execute(
                text("SELECT level FROM escalations WHERE level = 'human_requested' LIMIT 1")
            )
        finally:
            await transaction.rollback()
    missing_columns = _REQUIRED_COLUMNS - set(columns)
    missing_indexes = _REQUIRED_INDEXES - indexes
    if missing_columns or missing_indexes or columns.get(("escalations", "level")) != "YES":
        raise RuntimeError("schema_assurance_failed")
    return {
        "init_runs": 2,
        "required_columns": len(_REQUIRED_COLUMNS),
        "required_indexes": len(_REQUIRED_INDEXES),
        "historical_level_readable": True,
        "escalation_level_nullable": True,
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

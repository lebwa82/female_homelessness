from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db
from scripts.postgres_assurance import _REQUIRED_COLUMNS, _REQUIRED_INDEXES, assure


@pytest.mark.asyncio
async def test_assurance_inserts_reads_and_rolls_back_temporary_historical_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    init_runs = 0

    class Result:
        def __init__(self, rows=(), scalar=None):  # type: ignore[no-untyped-def]
            self._rows = rows
            self._scalar = scalar

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self._rows)

        def scalar_one(self):  # type: ignore[no-untyped-def]
            return self._scalar

    class Transaction:
        async def rollback(self) -> None:
            order.append("rollback")

    class Connection:
        async def begin(self) -> Transaction:
            return Transaction()

        async def execute(self, statement, parameters=None):  # type: ignore[no-untyped-def]
            del parameters
            sql = " ".join(str(statement).split())
            if "information_schema.columns" in sql:
                order.append("metadata")
                return Result(
                    tuple(
                        SimpleNamespace(table_name=table, column_name=column, is_nullable="YES")
                        for table, column in _REQUIRED_COLUMNS
                    )
                )
            if "pg_indexes" in sql:
                order.append("indexes")
                return Result(
                    tuple(
                        SimpleNamespace(
                            indexname=name,
                            indexdef=("CREATE UNIQUE INDEX" if name.startswith("uq_") else "CREATE INDEX"),
                        )
                        for name in _REQUIRED_INDEXES
                    )
                )
            if "INSERT INTO conversations" in sql:
                order.append("conversation_insert")
                return Result(scalar=41)
            if "INSERT INTO escalations" in sql:
                order.append("escalation_insert")
                return Result()
            if "SELECT level FROM escalations" in sql:
                order.append("historical_level_select")
                return Result(scalar="human_requested")
            if "INSERT INTO inbound_text_executions" in sql:
                order.append("claim_insert")
                return Result()
            if "SELECT status FROM inbound_text_executions" in sql:
                order.append("claim_select")
                return Result(scalar="processing")
            if "INSERT INTO conversation_messages" in sql:
                order.append("expired_message_insert")
                return Result()
            if "SELECT count(*) FROM conversation_messages" in sql:
                order.append("expired_message_select")
                return Result(scalar=1)
            if "DELETE FROM conversation_messages" in sql:
                order.append("message_delete")
                return Result()
            if "DELETE FROM inbound_text_executions" in sql:
                order.append("claim_delete")
                return Result()
            if "DELETE FROM escalations" in sql:
                order.append("escalation_delete")
                return Result()
            if "DELETE FROM conversations" in sql:
                order.append("conversation_delete")
                return Result()
            raise AssertionError("unexpected_statement")

    class Connect:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            del exc_type, exc, traceback

    class Engine:
        def connect(self) -> Connect:
            return Connect()

    async def init_db() -> None:
        nonlocal init_runs
        init_runs += 1

    monkeypatch.setattr(db, "engine", Engine())
    monkeypatch.setattr(db, "init_db", init_db)

    result = await assure()

    assert init_runs == 2
    assert result["historical_level_readable"] is True
    assert order == [
        "metadata",
        "indexes",
        "conversation_insert",
        "escalation_insert",
        "historical_level_select",
        "claim_insert",
        "claim_select",
        "expired_message_insert",
        "expired_message_select",
        "message_delete",
        "claim_delete",
        "escalation_delete",
        "conversation_delete",
        "rollback",
    ]

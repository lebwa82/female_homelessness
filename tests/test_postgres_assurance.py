from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db
from scripts.postgres_assurance import (
    _REQUIRED_COLUMNS,
    _index_projection,
    assure,
    expected_indexes,
)


def _definition(name: str) -> str:
    expected = expected_indexes[name]
    unique = "UNIQUE " if expected.unique else ""
    columns = ", ".join(expected.columns)
    predicate = f" WHERE {expected.predicate}" if expected.predicate else ""
    return f"CREATE {unique}INDEX {name} ON public.{expected.table} USING btree ({columns}){predicate}"


def test_assurance_rejects_a_correctly_named_index_with_wrong_column() -> None:
    expected = expected_indexes["uq_inbound_text_executions_origin"]

    assert _index_projection(
        "CREATE UNIQUE INDEX uq_inbound_text_executions_origin "
        "ON public.inbound_text_executions USING btree (conversation_id, lease_token)"
    ) != expected


@pytest.mark.asyncio
async def test_assurance_uses_rollback_bound_repository_paths(monkeypatch: pytest.MonkeyPatch) -> None:
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

        def scalar_one_or_none(self):  # type: ignore[no-untyped-def]
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
                return Result(tuple(SimpleNamespace(indexname=name, indexdef=_definition(name)) for name in expected_indexes))
            if "INSERT INTO conversations" in sql:
                order.append("conversation")
                return Result(scalar=41)
            if "SELECT inbound_text_executions.outcome" in sql:
                order.append("outcome")
                return Result(scalar={"text": "assurance", "choices": []})
            if "SELECT conversation_messages.id" in sql:
                order.append("retention")
                return Result(scalar=None)
            order.append("repository")
            return Result()

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
    assert result["claim_reclaim_outcome"] is True
    assert result["retention_purge_read"] is True
    assert result["comprehensive_delete"] is True
    assert order[:2] == ["metadata", "indexes"]
    assert {"conversation", "outcome", "retention", "rollback"} <= set(order)

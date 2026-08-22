from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db
from scripts import postgres_assurance
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

    assert _index_projection(
        "CREATE UNIQUE INDEX uq_inbound_text_executions_origin "
        "ON public.inbound_text_executions USING hash (conversation_id, source_message_id)"
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

    class Session:
        async def close(self) -> None:
            order.append("session_close")

    session = Session()
    claims = iter(("first", "second", "delivery"))
    saved: dict[str, object] = {}

    def repository_call(name: str) -> None:
        assert db._BOUND_REPOSITORY_SESSION.get() is session
        order.append(name)

    async def get_or_create(*_: object) -> object:
        repository_call("conversation")
        return SimpleNamespace(id=41, generation=0)

    async def claim(*_: object) -> str:
        repository_call("claim")
        return next(claims)

    async def truthy(name: str, *_: object) -> bool:
        repository_call(name)
        return True

    async def counted(name: str, *_: object) -> int:
        repository_call(name)
        return 2

    async def save(*args: object) -> bool:
        repository_call("save")
        saved.update(args[-1])  # type: ignore[arg-type]
        return True

    async def load(*_: object) -> tuple[dict[str, object], bool]:
        repository_call("load")
        return saved, False

    async def history(*_: object) -> list[object]:
        repository_call("history")
        return []

    async def seed_legacy(*_: object) -> int:
        repository_call("append")
        repository_call("legacy_seed")
        return 51

    async def assert_tombstone(*_: object) -> None:
        repository_call("tombstone")

    first_job = SimpleNamespace(id=61, lease_token="followup-first")
    second_job = SimpleNamespace(id=61, lease_token="followup-second")
    job_claims = iter(([first_job], [second_job]))

    class JobRepository:
        async def claim_due_jobs(self, *_: object) -> list[object]:
            repository_call("job_claim")
            return next(job_claims)

        async def complete_job(self, job: object, success: bool) -> None:
            assert job is first_job and success is False
            repository_call("job_release")

        async def discard_job(self, job: object) -> None:
            assert job is second_job
            repository_call("job_discard")

    async def nothing(name: str, *_: object, **__: object) -> None:
        repository_call(name)

    monkeypatch.setattr(db, "engine", Engine())
    monkeypatch.setattr(db, "init_db", init_db)
    monkeypatch.setattr(postgres_assurance, "AsyncSession", lambda **_: session)
    monkeypatch.setattr(db, "get_or_create_conversation_record", get_or_create)
    monkeypatch.setattr(db, "claim_text_execution", claim)
    monkeypatch.setattr(db, "fail_text_execution", lambda *args: truthy("fail", *args))
    monkeypatch.setattr(db, "save_text_execution_outcome", save)
    monkeypatch.setattr(db, "load_text_execution_outcome", load)
    monkeypatch.setattr(db, "claim_text_execution_delivery", claim)
    monkeypatch.setattr(db, "acknowledge_text_execution_outcome", lambda *args: nothing("ack", *args))
    monkeypatch.setattr(postgres_assurance, "_seed_legacy_followup_and_null_retention", seed_legacy)
    monkeypatch.setattr(postgres_assurance, "PostgresJobRepository", JobRepository)
    monkeypatch.setattr(db, "purge_expired_content", lambda *args: counted("purge", *args))
    monkeypatch.setattr(db, "load_history", history)
    monkeypatch.setattr(db, "load_active_contact_points", history)
    monkeypatch.setattr(db, "delete_conversation_data", lambda *args: nothing("delete", *args))
    monkeypatch.setattr(postgres_assurance, "_assert_delete_tombstone", assert_tombstone)

    result = await assure()

    assert init_runs == 2
    assert result["claim_reclaim_outcome"] is True
    assert result["followup_claim_reclaim"] is True
    assert result["null_retention_purge"] is True
    assert result["retention_purge_read"] is True
    assert result["comprehensive_delete"] is True
    assert result["delete_tombstone"] is True
    assert order[:2] == ["metadata", "indexes"]
    assert {
        "conversation",
        "claim",
        "fail",
        "save",
        "load",
        "ack",
        "append",
        "legacy_seed",
        "job_claim",
        "job_release",
        "job_discard",
        "purge",
        "history",
        "delete",
        "tombstone",
        "session_close",
        "rollback",
    } <= set(order)

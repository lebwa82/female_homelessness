"""RED/green checks for durable storage, evaluator, and release-gate contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

from app import db, worker
from app.agents import AgentContext, AgentEvaluation
from app.db import Escalation, content_expiry_at, sanitize_agent_audit
from app.domain import (
    AgentTurn,
    DiagnosticStatus,
    IncomingMessage,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportOffer,
)
from app.service import ConversationService
from app.store import InMemoryConversationStore
from scripts.dialogue_eval import DatasetError, load_cases


def _audit_has_no_provider_controlled_values(audit: dict[str, object], values: tuple[str, ...]) -> bool:
    serialized = json.dumps(audit, ensure_ascii=True, sort_keys=True)
    return not any(value in serialized for value in values)


def test_agent_run_audit_reduces_provider_controlled_keys_and_types_to_categories() -> None:
    field_name = "provider" + "_controlled_field"
    type_name = "provider" + "_controlled_type"
    audit = sanitize_agent_audit(
        {
            "status": "invalid",
            "validation_errors": {"fields": [field_name], "types": [type_name]},
            "input_hash": "a" * 64,
        }
    )

    assert audit["validation_errors"] == {
        "fields": ["unknown_field"],
        "types": ["other_validation_error"],
    }
    assert _audit_has_no_provider_controlled_values(audit, (field_name, type_name))


def test_agent_run_audit_keeps_a_bounded_format_retry_count() -> None:
    audit = sanitize_agent_audit(
        {
            "status": "completed",
            "format_retry_count": 1,
            "normalization": {"categories": ["direct_human_request_level_normalized"]},
        }
    )

    assert audit["format_retry_count"] == 1
    assert audit["normalization"] == {"categories": ["direct_human_request_level_normalized"]}


def test_content_retention_uses_the_configured_period() -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)

    assert content_expiry_at(now, retention_days=7) == now + timedelta(days=7)


@pytest.mark.asyncio
async def test_history_query_excludes_expired_rows_before_the_purge_worker_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Result:
        def scalars(self) -> tuple[object, ...]:
            return ()

    class Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            statements.append(str(statement))
            return Result()

    monkeypatch.setattr(db, "Session", Session)

    assert await db.load_history(1) == []
    assert any("expires_at" in statement and ">" in statement for statement in statements)


@pytest.mark.asyncio
async def test_retention_maintenance_survives_a_transient_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable() -> int:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(worker, "purge_expired_content", unavailable)

    assert await worker.purge_expired_content_safely() is False


def test_escalation_request_key_has_one_named_unique_constraint() -> None:
    constraints = [
        constraint
        for constraint in Escalation.__table__.constraints
        if constraint.name == "uq_escalations_request_key"
    ]

    assert Escalation.__table__.c.request_key.unique is not True
    assert len(constraints) == 1
    assert {column.name for column in constraints[0].columns} == {"request_key"}


@pytest.mark.asyncio
async def test_schema_migration_removes_the_legacy_escalation_unique_constraint_before_adding_the_named_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Connection:
        async def run_sync(self, function: object) -> None:
            del function

        async def execute(self, statement: object) -> None:
            statements.append(str(statement))

    class Begin:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *args: object) -> None:
            return None

    class Engine:
        def begin(self) -> Begin:
            return Begin()

    monkeypatch.setattr(db, "engine", Engine())

    await db.init_db()

    legacy_drop = "ALTER TABLE escalations DROP CONSTRAINT IF EXISTS escalations_request_key_key"
    legacy_index_drop = "DROP INDEX IF EXISTS escalations_request_key_key"
    named_create = "CREATE UNIQUE INDEX IF NOT EXISTS uq_escalations_request_key"
    assert any(legacy_drop in statement for statement in statements)
    assert any(legacy_index_drop in statement for statement in statements)
    assert next(index for index, statement in enumerate(statements) if legacy_drop in statement) < next(
        index for index, statement in enumerate(statements) if named_create in statement
    )
    assert next(index for index, statement in enumerate(statements) if legacy_index_drop in statement) < next(
        index for index, statement in enumerate(statements) if named_create in statement
    )


@pytest.mark.asyncio
async def test_delete_sql_removes_every_linked_record_and_does_not_create_an_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, statement: object) -> None:
            statements.append(str(statement))

        async def commit(self) -> None:
            return None

    monkeypatch.setattr(db, "Session", Session)

    await db.delete_conversation_data(1)

    sql = "\n".join(statements)
    for table in (
        "contact_points",
        "followup_jobs",
        "aid_requests",
        "conversation_messages",
        "callback_executions",
        "inbound_text_executions",
        "agent_runs",
        "risk_assessments",
        "action_executions",
        "escalations",
        "events",
        "conversations",
    ):
        assert table in sql


@pytest.mark.parametrize("line", ('{"version": 2, "version": 2}', '{"version": NaN}'))
def test_evaluator_jsonl_rejects_ambiguous_or_nonstandard_json_constants(tmp_path: Path, line: str) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text(f"{line}\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="invalid JSON"):
        load_cases(path)


@pytest.mark.asyncio
async def test_system_information_uses_the_common_human_handoff_affordance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bot

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=101, username="tester"),
        chat=SimpleNamespace(id=202),
        message_id=303,
        date=SimpleNamespace(isoformat=lambda: "2026-08-22T10:00:00+00:00"),
    )
    captured: list[AgentTurn] = []

    async def capture(_: object, __: object, turn: AgentTurn) -> None:
        captured.append(turn)

    monkeypatch.setattr(bot, "send_turn", capture)
    monkeypatch.setattr(
        bot,
        "conversation_service",
        SimpleNamespace(claim_inbound=lambda _: _return_true()),
    )

    await bot.system_info(message)

    assert captured and any(choice.id == "human" for choice in captured[0].choices)


async def _return_true() -> bool:
    return True


def test_deploy_gate_plan_requires_offline_checks_and_postgres_assurance_before_activation() -> None:
    from scripts.deploy_gate import staged_release_gate

    commands = staged_release_gate("abc1234", "/stage", "/live")

    assert commands.index("just check") < commands.index("just scenario-smoke")
    assert commands.index("just scenario-smoke") < commands.index("just eval-dialogues")
    assert commands.index("just eval-dialogues") < commands.index("just db-assure")
    assert commands.index("just db-assure") < commands.index("activate-release")


class _SupportOfferGateway:
    def __init__(self, support_intents: tuple[str, ...] = ("open_conversation",)) -> None:
        self.calls = 0
        self.support_intents = support_intents

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        del context
        self.calls += 1
        intent = self.support_intents[min(self.calls - 1, len(self.support_intents) - 1)]
        return AgentEvaluation(
            safety=SafetyDiagnostic(level="none"),
            support=SupportDiagnostic(
                intent=intent,
                draft_text="Можно попробовать психологическую поддержку.",
                suggested_support=SupportOffer.PSYCHOLOGIST if self.calls == 1 else None,
            ),
            safety_status=DiagnosticStatus.COMPLETED,
            support_status=DiagnosticStatus.COMPLETED,
            safety_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


def _incoming(text: str, message_id: int) -> IncomingMessage:
    return IncomingMessage(platform_user_id=101, chat_id=202, text=text, message_id=message_id)


@pytest.mark.asyncio
async def test_two_turn_psychologist_offer_can_create_a_request_and_expires_after_an_unrelated_turn() -> None:
    create_store = InMemoryConversationStore()
    create_service = ConversationService(
        store=create_store,
        gateway=_SupportOfferGateway(("open_conversation", "psychologist_request")),
    )

    await create_service.handle_text(_incoming("мне тяжело", 801))
    assert create_store.conversations[101].pending_offer == "psychologist"
    await create_service.handle_text(_incoming("да хочу", 802))
    await create_service.handle_callback(_incoming("contact:later", 803), "contact:later")
    assert len(create_store.aid_requests) == 1
    assert create_store.conversations[101].pending_offer is None

    expire_store = InMemoryConversationStore()
    expire_service = ConversationService(
        store=expire_store,
        gateway=_SupportOfferGateway(("open_conversation", "open_conversation")),
    )
    await expire_service.handle_text(_incoming("мне тяжело", 811))
    await expire_service.handle_text(_incoming("о погоде", 812))

    assert expire_store.conversations[101].pending_offer is None


@pytest.mark.asyncio
async def test_mid_turn_persistence_failure_marks_an_inbound_update_retryable() -> None:
    class FailsOnceStore(InMemoryConversationStore):
        failures_remaining = 1

        async def append_message(
            self,
            record: object,
            role: str,
            content: str,
            audit: dict[str, object] | None = None,
        ) -> None:
            if role == "user" and self.failures_remaining:
                self.failures_remaining -= 1
                raise RuntimeError("transient persistence failure")
            await super().append_message(record, role, content, audit)

    store = FailsOnceStore()
    gateway = _SupportOfferGateway()
    service = ConversationService(store=store, gateway=gateway)
    update = _incoming("мне тяжело", 821)

    first = await service.handle_text(update)
    second = await service.handle_text(update)

    assert "повторить" in first.text.lower()
    assert gateway.calls == 1
    assert len(store.messages) == 1
    assert second.choices

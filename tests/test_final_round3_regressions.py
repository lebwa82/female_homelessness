"""Focused RED/GREEN regressions for final review fix round 3.

All interactions use in-memory doubles and synthetic text; no provider,
Telegram, Podman, deployment, or database is contacted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot
from app.agents import AgentCallResult, YandexAgentGateway
from app.domain import (
    AgentTurn,
    ConversationState,
    IncomingMessage,
    PolicyEffect,
)
from app.service import ConversationService
from app.store import ConversationRecord, InMemoryConversationStore
from app.worker import DueJob, run_due_jobs


def incoming(text: str, message_id: int = 1001) -> IncomingMessage:
    return IncomingMessage(platform_user_id=501, chat_id=502, text=text, message_id=message_id)


@pytest.mark.asyncio
async def test_finish_uses_common_cleanup_and_cancels_processing_reminder() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(incoming("", 1003))
    await store.update(
        record,
        state=ConversationState.AID_REQUESTED.value,
        pending_aid_id="legal_consultation",
        pending_contact_method="phone",
        pending_city="synthetic-city",
    )
    store.followup_jobs.append(
        SimpleNamespace(conversation_id=record.id, aid_request_id=1, status="processing")
    )

    await service.handle_callback(incoming("finish", 1004), "finish")

    assert record.state == ConversationState.CLOSED.value
    assert record.pending_aid_id is None
    assert store.followup_jobs == []


def test_one_update_identity_is_independent_from_derived_effect() -> None:
    record = ConversationRecord(71, "telegram", 501, 502, None)

    assert ConversationService._text_request_key(record, 1005, PolicyEffect.NONE) == ConversationService._text_request_key(
        record, 1005, PolicyEffect.CRITICAL_ESCALATION
    )


@pytest.mark.asyncio
async def test_confirmed_delivery_denial_suppresses_stale_turn_before_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(answer=AsyncMock())
    service = SimpleNamespace(
        authorize_delivery=AsyncMock(return_value=False),
        record_outbound=AsyncMock(),
    )
    monkeypatch.setattr(bot, "conversation_service", service)
    turn = AgentTurn(text="synthetic", audit={"conversation_id": 1, "conversation_generation": 2})

    await bot.send_turn(message, incoming("synthetic", 1006), turn)

    message.answer.assert_not_awaited()
    service.record_outbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_creates_a_generation_tombstone_and_new_record_cannot_reuse_it() -> None:
    store = InMemoryConversationStore()
    first = await store.ensure(incoming("", 1007))

    await store.delete_data(first)
    replacement = await store.ensure(incoming("", 1008))

    assert replacement.id != first.id
    assert replacement.generation == first.generation + 1


@pytest.mark.asyncio
async def test_confirmed_tombstone_denies_a_bound_stale_delivery() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(incoming("", 1009))
    stale_turn = AgentTurn(
        text="synthetic",
        audit={"conversation_id": record.id, "conversation_generation": record.generation},
    )

    await store.delete_data(record)

    assert await service.authorize_delivery(incoming("", 1009), stale_turn) is False


@pytest.mark.asyncio
async def test_tombstone_binding_marks_a_completed_stale_turn_non_deliverable() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(incoming("", 1011))
    turn = AgentTurn(text="synthetic")

    await store.delete_data(record)
    bound = await service._bind_turn_to_current_record(incoming("", 1011), turn)

    assert bound.audit["suppress_delivery"] is True


@pytest.mark.asyncio
async def test_outbound_acknowledgement_precedes_optional_assistant_audit() -> None:
    class AuditFailureStore(InMemoryConversationStore):
        async def append_message(self, record, role, content, audit=None):  # type: ignore[no-untyped-def]
            if role == "assistant":
                raise RuntimeError("synthetic audit failure")
            await super().append_message(record, role, content, audit)

    store = AuditFailureStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(incoming("", 1010))
    claim = await store.claim_text(record, 1010)
    assert claim is not None
    turn = AgentTurn(text="synthetic", audit={"conversation_id": record.id, "conversation_generation": record.generation})
    await store.save_text_outcome(record, 1010, claim, turn)

    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await service.record_outbound(incoming("", 1010), turn)

    assert (await store.load_text_outcome(record, 1010))[1] is True


@pytest.mark.asyncio
async def test_worker_releases_a_claim_when_delivery_revalidation_fails() -> None:
    class RevalidationFailure:
        def __init__(self) -> None:
            self.completed: list[bool] = []

        async def claim_due_jobs(self, now):  # type: ignore[no-untyped-def]
            return [DueJob(id=1, conversation_id=2, chat_id=3, kind="followup", lease_token="lease")]

        async def can_deliver(self, job):  # type: ignore[no-untyped-def]
            raise RuntimeError("synthetic revalidation failure")

        async def complete_job(self, job, success):  # type: ignore[no-untyped-def]
            self.completed.append(success)

        async def discard_job(self, job):  # type: ignore[no-untyped-def]
            raise AssertionError("revalidation failure must be reclaimed, not discarded")

    repository = RevalidationFailure()

    assert await run_due_jobs(AsyncMock(), repository) == 0
    assert repository.completed == [False]


@pytest.mark.asyncio
async def test_worker_holds_delivery_authorization_through_the_send() -> None:
    """Cancellation can serialize before delivery, never in a check/send gap."""

    class Repository:
        def __init__(self) -> None:
            self.authorization_events: list[str] = []

        async def claim_due_jobs(self, now):  # type: ignore[no-untyped-def]
            return [DueJob(id=1, conversation_id=2, chat_id=3, kind="followup", lease_token="lease")]

        @asynccontextmanager
        async def delivery_authorization(self, job):  # type: ignore[no-untyped-def]
            self.authorization_events.append("entered")
            try:
                yield True
            finally:
                self.authorization_events.append("released")

        async def complete_job(self, job, success):  # type: ignore[no-untyped-def]
            raise AssertionError("durable authorization owns completion")

    class Bot:
        async def send_message(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            assert repository.authorization_events == ["entered"]

    repository = Repository()

    assert await run_due_jobs(Bot(), repository) == 1
    assert repository.authorization_events == ["entered", "released"]


def test_worker_defines_processing_lease_and_closed_state_guard() -> None:
    source = Path("app/worker.py").read_text(encoding="utf-8")

    assert "FOLLOWUP_PROCESSING_LEASE" in source
    assert "ConversationState.CLOSED.value" in source


def test_staged_non_database_gates_use_synthetic_database_not_service_secret() -> None:
    source = Path("scripts/deploy_prod.sh").read_text(encoding="utf-8")

    assert "DATABASE_URL=postgresql+asyncpg://offline" in source
    assert "run_staged just db-assure" not in source
    assert "run_staged_db_assure" in source


def test_postgres_assurance_validates_index_columns_and_predicates() -> None:
    source = Path("scripts/postgres_assurance.py").read_text(encoding="utf-8")

    assert "indexdef" in source
    assert "expected_indexes" in source
    assert "wrong_index_definition" in source


def _gateway() -> YandexAgentGateway:
    async def call(name: str, _: str, __: str) -> AgentCallResult:
        if name == "risk":
            return AgentCallResult(payload={"level": "none", "rationale": "synthetic"}, audit={"status": "completed"})
        return AgentCallResult(payload={"intent": "open_conversation", "draft_text": "synthetic"}, audit={"status": "completed"})

    return YandexAgentGateway(call=call)

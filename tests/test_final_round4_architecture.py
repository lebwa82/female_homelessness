"""Round-4 regressions for atomic processing and durable delivery.

All adapters are synthetic; this module never contacts Telegram, a provider,
or a database.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot, db, worker
from app.agents import AgentCallResult, YandexAgentGateway
from app.domain import AgentTurn, ConversationState, InboundExecutionKey, IncomingMessage
from app.service import PERSISTENCE_UNAVAILABLE_PROMPT, ConversationService
from app.store import ConversationRecord, InMemoryConversationStore, PostgresConversationStore
from app.worker import DueJob, PostgresJobRepository


def incoming(text: str, message_id: int = 1101) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=3101,
        chat_id=4101,
        username="synthetic",
        text=text,
        message_id=message_id,
    )


def gateway(calls: list[str] | None = None) -> YandexAgentGateway:
    async def call(name: str, _: str, __: str) -> AgentCallResult:
        if calls is not None:
            calls.append(name)
        if name == "risk":
            return AgentCallResult(
                payload={"level": "none", "rationale": "synthetic"},
                audit={"status": "completed"},
            )
        return AgentCallResult(
            payload={"intent": "open_conversation", "draft_text": "synthetic"},
            audit={"status": "completed"},
        )

    return YandexAgentGateway(call=call)


@pytest.mark.asyncio
async def test_post_effect_failure_rolls_back_the_entire_inbound_unit_then_retries_once() -> None:
    class FailingOutcomeStore(InMemoryConversationStore):
        fail_once = True

        async def save_text_outcome(self, *args: object, **kwargs: object) -> None:
            await super().save_text_outcome(*args, **kwargs)  # type: ignore[arg-type]
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("synthetic outcome boundary")

    calls: list[str] = []
    store = FailingOutcomeStore()
    service = ConversationService(store=store, gateway=gateway(calls))
    record = await store.ensure(incoming("", 1100))
    await store.update(
        record,
        state=ConversationState.COLLECTING_CONTACT_VALUE.value,
        need="legal",
        pending_aid_id="legal_consultation",
        pending_contact_method="phone",
    )

    first = await service.handle_text(incoming("+7 900 000 00 00"))

    current = await store.get(incoming("", 1100))
    assert first.text == PERSISTENCE_UNAVAILABLE_PROMPT
    assert current is not None
    assert current.state == ConversationState.COLLECTING_CONTACT_VALUE.value
    assert current.pending_aid_id == "legal_consultation"
    assert store.aid_requests == []
    assert store.followup_jobs == []
    assert store.actions == []
    assert store.text_outcomes == {}

    second = await service.handle_text(incoming("+7 900 000 00 00"))

    assert second.text != PERSISTENCE_UNAVAILABLE_PROMPT
    assert len(store.aid_requests) == 1
    assert len(store.followup_jobs) == 1
    assert sum(kind == "create_aid_request" for _, kind, _, _ in store.actions) == 1
    assert sorted(calls) == ["risk", "risk", "support", "support"]


@pytest.mark.asyncio
async def test_saving_an_outcome_atomically_completes_the_inbound_claim() -> None:
    class CompletionTrapStore(InMemoryConversationStore):
        async def complete_text(self, *_: object, **__: object) -> None:
            raise AssertionError("save_text_outcome must finalize the claim atomically")

    store = CompletionTrapStore()
    turn = await ConversationService(store=store, gateway=gateway()).handle_text(incoming("мне тяжело", 1102))

    assert turn.text != PERSISTENCE_UNAVAILABLE_PROMPT
    record = await store.get(incoming("", 1102))
    assert record is not None
    claim = store.text_claims[(record.id, "1102")]
    assert claim.status == "completed"


@pytest.mark.asyncio
async def test_two_service_instances_racing_one_contact_update_commit_one_effect_and_outcome() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def call(name: str, _: str, __: str) -> AgentCallResult:
        started.append(name)
        await release.wait()
        if name == "risk":
            return AgentCallResult(
                payload={"level": "none", "rationale": "synthetic"},
                audit={"status": "completed"},
            )
        return AgentCallResult(
            payload={"intent": "open_conversation", "draft_text": "synthetic"},
            audit={"status": "completed"},
        )

    store = InMemoryConversationStore()
    first = ConversationService(store=store, gateway=YandexAgentGateway(call=call))
    second = ConversationService(store=store, gateway=YandexAgentGateway(call=call))
    update = incoming("+7 900 000 00 00", 1107)
    record = await store.ensure(update)
    await store.update(
        record,
        state=ConversationState.COLLECTING_CONTACT_VALUE.value,
        need="legal",
        pending_aid_id="legal_consultation",
        pending_contact_method="phone",
    )

    first_task = asyncio.create_task(first.handle_text(update))
    while len(started) < 2:
        await asyncio.sleep(0)
    second_task = asyncio.create_task(second.handle_text(update))
    release.set()
    await asyncio.gather(first_task, second_task)

    assert sorted(started) == ["risk", "support"]
    assert len(store.aid_requests) == 1
    assert len(store.followup_jobs) == 1
    assert sum(kind == "create_aid_request" for _, kind, _, _ in store.actions) == 1
    assert len(store.text_outcomes) == 1


@pytest.mark.asyncio
async def test_callback_effect_and_outcome_commit_together_and_replay_verbatim() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming("human", 1108)
    record = await store.ensure(update)
    await store.update(record, state=ConversationState.OPEN_CONVERSATION.value)

    first = await service.handle_callback(update, "human")
    replay = await service.handle_callback(update, "human")

    assert replay.text == first.text
    assert len(store.escalations) == 1
    assert sum(kind == "human_handoff" for _, kind, _, _ in store.actions) == 1
    assert len(store.text_outcomes) == 1
    stored = await store.load_text_outcome(record, InboundExecutionKey.callback(update.message_id))
    assert stored is not None and stored[0].text == first.text


@pytest.mark.asyncio
async def test_callback_outcome_identity_does_not_collide_with_text_message_identity() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming("хочу поговорить с психологом", 1109)

    contact = await service.handle_text(update)
    assert any(choice.id == "contact:current_telegram" for choice in contact.choices)

    completed = await service.handle_callback(update, "contact:current_telegram")

    assert completed.text != contact.text
    assert store.aid_requests[-1].aid_id == "psychologist_3_sessions"
    assert len(store.text_outcomes) == 2


@pytest.mark.asyncio
async def test_postgres_outcome_payload_persists_criticality_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def save(
        conversation_id: int,
        message_id: int | None,
        lease_token: str,
        outcome: dict[str, object],
    ) -> bool:
        del conversation_id, message_id, lease_token
        captured.update(outcome)
        return True

    monkeypatch.setattr(db, "save_text_execution_outcome", save)
    record = ConversationRecord(71, "telegram", 3101, 4101, None, generation=9)
    turn = AgentTurn(text="critical", audit={"critical_delivery": True})

    await PostgresConversationStore("synthetic-key").save_text_outcome(record, 1103, "lease", turn)

    assert captured["critical_delivery"] is True
    assert captured["conversation_generation"] == 9


@pytest.mark.asyncio
async def test_critical_delivery_fails_open_only_when_authorization_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def unavailable(_: IncomingMessage, __: AgentTurn):
        raise RuntimeError("synthetic auth outage")
        yield  # pragma: no cover

    message = SimpleNamespace(answer=AsyncMock())
    service = SimpleNamespace(delivery_authorization=unavailable, record_outbound=AsyncMock())
    monkeypatch.setattr(bot, "conversation_service", service)
    turn = AgentTurn(
        text="critical",
        audit={"critical_delivery": True, "conversation_id": 1, "conversation_generation": 0},
    )

    await bot.send_turn(message, incoming("synthetic", 1104), turn)

    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmed_deletion_denies_even_a_critical_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def denied(_: IncomingMessage, __: AgentTurn):
        yield None

    message = SimpleNamespace(answer=AsyncMock())
    service = SimpleNamespace(delivery_authorization=denied, record_outbound=AsyncMock())
    monkeypatch.setattr(bot, "conversation_service", service)
    turn = AgentTurn(
        text="critical",
        audit={"critical_delivery": True, "conversation_id": 1, "conversation_generation": 0},
    )

    await bot.send_turn(message, incoming("synthetic", 1105), turn)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_and_replayed_canonical_critical_turns_fail_open_on_auth_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming("не хочу жить", 1110)
    initial = await service.handle_text(update)
    replay = await service.handle_text(update)

    @asynccontextmanager
    async def unavailable(_: IncomingMessage, __: AgentTurn):
        raise RuntimeError("synthetic auth outage")
        yield  # pragma: no cover

    adapter = SimpleNamespace(delivery_authorization=unavailable, record_outbound=AsyncMock())
    monkeypatch.setattr(bot, "conversation_service", adapter)
    message = SimpleNamespace(answer=AsyncMock())

    await bot.send_turn(message, update, initial)
    await bot.send_turn(message, update, replay)

    assert initial.audit["critical_delivery"] is True
    assert replay.audit["critical_delivery"] is True
    assert message.answer.await_count == 2


@pytest.mark.asyncio
async def test_confirmed_delete_denies_initial_and_replayed_canonical_critical_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming("не хочу жить", 1111)
    initial = await service.handle_text(update)
    replay = await service.handle_text(update)
    await service.delete(incoming("/delete", 1112))
    monkeypatch.setattr(bot, "conversation_service", service)
    message = SimpleNamespace(answer=AsyncMock())

    await bot.send_turn(message, update, initial)
    await bot.send_turn(message, update, replay)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_is_reclaimed_by_independent_outbox_without_diagnostics_or_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AuditFailureStore(InMemoryConversationStore):
        async def append_message(self, record, role, content, audit=None):  # type: ignore[no-untyped-def]
            if role == "assistant":
                raise RuntimeError("synthetic optional audit failure")
            await super().append_message(record, role, content, audit)

    calls: list[str] = []
    store = AuditFailureStore()
    service = ConversationService(store=store, gateway=gateway(calls))
    update = incoming("мне тяжело", 1106)
    turn = await service.handle_text(update)
    first_message = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError("synthetic send failure")))
    monkeypatch.setattr(bot, "conversation_service", service)

    await bot.send_turn(first_message, update, turn)

    worker_bot = SimpleNamespace(send_message=AsyncMock())
    assert await worker.run_pending_outcomes(worker_bot, service) == 1
    assert await worker.run_pending_outcomes(worker_bot, service) == 0
    worker_bot.send_message.assert_awaited_once()
    assert sorted(calls) == ["risk", "support"]
    record = await store.get(update)
    assert record is not None
    assert (await store.load_text_outcome(record, update.message_id))[1] is True


def test_followup_claim_reclaims_legacy_processing_row_with_null_lease() -> None:
    statement = worker.followup_claim_statement(datetime(2026, 8, 22, tzinfo=UTC))
    sql = " ".join(str(statement.compile(compile_kwargs={"literal_binds": True})).lower().split())

    assert "followup_jobs.lease_expires_at is null" in sql
    assert "followup_jobs.lease_expires_at <=" in sql


@pytest.mark.asyncio
async def test_terminal_followup_authorization_denial_cancels_inside_locked_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        id=11,
        status="processing",
        lease_token="lease",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        conversation_id=12,
        conversation_generation=3,
    )
    conversation = SimpleNamespace(
        id=12,
        chat_id=13,
        generation=3,
        state=ConversationState.CLOSED.value,
    )

    class Session:
        commits = 0

        async def __aenter__(self):
            self.rows = iter((conversation, row))
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def scalar(self, _: object) -> object:
            return next(self.rows)

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    monkeypatch.setattr(db, "repository_session", lambda: session)
    job = DueJob(11, 12, 13, "followup", conversation_generation=3, lease_token="lease")

    async with PostgresJobRepository().delivery_authorization(job) as allowed:
        assert allowed is False

    assert row.status == "cancelled"
    assert row.lease_token is None and row.lease_expires_at is None
    assert session.commits == 1


def test_production_deploy_has_no_environment_controlled_privileged_path() -> None:
    source = Path("scripts/deploy_prod.sh").read_text(encoding="utf-8")

    assert "WOMEN_HELP_STAGED_PATH" not in source
    assert 'readonly SUDO_BIN="/usr/bin/sudo"' in source
    assert 'readonly UV_BIN="/usr/local/bin/uv"' in source
    assert 'readonly JUST_BIN="/usr/local/bin/just"' in source
    assert "verify_root_tool" in source
    assert Path("scripts/deploy_prod_test_harness.sh").is_file()

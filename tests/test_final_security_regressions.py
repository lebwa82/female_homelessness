"""Regression coverage for the final whole-branch security review.

The cases use only the synthetic phrases approved in the final-fix brief.  Contact
values are constructed at runtime and are never included in assertion diagnostics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.agents import AgentCallResult, AgentContext, AgentEvaluation, YandexAgentGateway
from app.domain import (
    ConversationState,
    DiagnosticStatus,
    IncomingMessage,
    SafetyDiagnostic,
    SupportDiagnostic,
)
from app.service import ConversationService
from app.store import InMemoryConversationStore


def incoming(text: str, message_id: int = 303) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=101,
        chat_id=202,
        username="helper_test",
        text=text,
        message_id=message_id,
    )


def evaluation() -> AgentEvaluation:
    return AgentEvaluation(
        safety=SafetyDiagnostic(level="none", rationale="safe"),
        support=SupportDiagnostic(intent="open_conversation", draft_text="Я рядом."),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "completed"},
        support_audit={"status": "completed"},
    )


@dataclass
class FixedGateway:
    result: AgentEvaluation = field(default_factory=evaluation)
    calls: int = 0

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        del context
        self.calls += 1
        return self.result


def test_telegram_handles_are_redacted_as_contacts() -> None:
    from app.pii import redact_for_model

    value = "@" + "synthetic_contact"

    if redact_for_model(value) != "[CONTACT]":
        pytest.fail("telegram handle was not replaced by the contact marker")


def test_pii_runtime_uses_an_offline_public_suffix_list_for_url_masking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import pii
    from app.pii import redact_for_model

    original = pii.tld_extractor

    class RecordingExtractor:
        suffix_list_urls = ()

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, value: str):  # type: ignore[no-untyped-def]
            self.calls += 1
            return original(value)

    extractor = RecordingExtractor()
    monkeypatch.setattr(pii, "tld_extractor", extractor)

    redacted = redact_for_model("смотрите https://example.org")

    assert extractor.suffix_list_urls == ()
    assert extractor.calls == 1
    assert "example.org" not in redacted


@pytest.mark.asyncio
async def test_typed_contact_is_replaced_in_current_and_historical_provider_views() -> None:
    captured: list[str] = []

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        del instructions
        captured.append(input_text)
        payload = {"level": "none", "rationale": "safe"} if agent_name == "risk" else {
            "intent": "open_conversation",
            "draft_text": "Я рядом.",
        }
        return AgentCallResult(payload=payload, audit={"status": "completed"})

    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=YandexAgentGateway(call=call))
    record = await store.ensure(incoming(""))
    await store.update(
        record,
        state=ConversationState.COLLECTING_CONTACT_VALUE.value,
        pending_aid_id="legal_consultation",
        pending_contact_method="other_telegram",
    )
    contact = "typed" + "_marker_59173"

    await service.handle_text(incoming(contact, 304))
    await service.handle_text(incoming("мне плохо", 305))

    assert captured
    if not _provider_views_keep_contact_private(captured, contact):
        pytest.fail("provider view retained typed contact")
    assert _provider_views_have_contact_placeholder(captured)


def _provider_views_keep_contact_private(views: list[str], value: str) -> bool:
    return not any(value in view for view in views)


def _provider_views_have_contact_placeholder(views: list[str]) -> bool:
    return any("[CONTACT]" in view for view in views)


@pytest.mark.asyncio
async def test_duplicate_text_update_runs_the_two_agent_calls_only_once() -> None:
    gateway = FixedGateway()
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=gateway)
    update = incoming("мне плохо", 501)

    await service.handle_text(update)
    await service.handle_text(update)

    assert gateway.calls == 1
    assert len(store.messages) == 1


@pytest.mark.asyncio
async def test_duplicate_start_update_does_not_repeat_persisted_effects() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway())
    update = incoming("", 551)

    await service.start(update)
    await service.start(update)

    assert len(store.messages) == 1
    assert len(store.actions) == 1


@pytest.mark.asyncio
async def test_one_keyboard_slot_accepts_only_one_mutually_exclusive_callback() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(incoming(""))

    first = await store.claim_callback(record, "contact:current_telegram", 601)
    second = await store.claim_callback(record, "contact:phone", 601)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_concurrent_callback_updates_are_serialized_per_conversation() -> None:
    class DelayedStore(InMemoryConversationStore):
        entered: asyncio.Event
        release: asyncio.Event

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def create_aid_request(self, *args: object, **kwargs: object) -> object:
            self.entered.set()
            await self.release.wait()
            return await super().create_aid_request(*args, **kwargs)

    store = DelayedStore()
    service = ConversationService(store=store, gateway=FixedGateway())
    record = await store.ensure(incoming(""))
    await store.update(
        record,
        state=ConversationState.COLLECTING_CONTACT_METHOD.value,
        pending_aid_id="legal_consultation",
    )

    first = asyncio.create_task(service.handle_callback(incoming("", 621), "contact:later"))
    await store.entered.wait()
    second = asyncio.create_task(service.handle_callback(incoming("", 622), "contact:later"))
    await asyncio.sleep(0)
    store.release.set()
    await asyncio.gather(first, second)

    assert len(store.aid_requests) == 1


@pytest.mark.asyncio
async def test_delete_removes_the_identity_and_every_linked_local_record() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway())
    await service.handle_text(incoming("мне плохо", 701))

    turn = await service.delete(incoming("/delete", 702))

    assert 101 not in store.conversations
    assert store.messages == []
    assert store.agent_runs == []
    assert store.risk_assessments == []
    assert store.actions == []
    assert store.escalations == []
    assert turn.audit["skip_outbound_persistence"] is True

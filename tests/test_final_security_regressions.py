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
    HardSignalKind,
    IncomingMessage,
    PolicyContext,
    PolicyEffect,
    RiskLevel,
    SafetyDiagnostic,
    SupportDiagnostic,
)
from app.policy import resolve_turn
from app.safety import assess_local_risk, assess_local_risk_from_signals
from app.service import ConversationService
from app.signals import extract_signals
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


def _has_kind(text: str, kind: HardSignalKind) -> bool:
    return any(match.kind is kind for match in extract_signals(text).matches)


def _decision(text: str) -> PolicyEffect:
    signals = extract_signals(text)
    return resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.UNAVAILABLE,
        )
    ).effect


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


@pytest.mark.parametrize(
    "text",
    (
        "я хочу умереть",
        "я убью себя",
        "хочу сейчас причинить себе вред",
        "не хочу жить, помогите",
    ),
)
def test_direct_suicidal_language_routes_critical(text: str) -> None:
    assert assess_local_risk(text).level is RiskLevel.CRITICAL


@pytest.mark.parametrize("text", ("не хочу жить в этом городе", "не хочу жить с ним"))
def test_suicide_rules_keep_residence_and_relationship_near_misses_open(text: str) -> None:
    assert assess_local_risk(text).level is RiskLevel.NONE


@pytest.mark.parametrize(
    "text",
    (
        "он сейчас меня бьёт, я не могу уйти",
        "он сейчас меня бьет, и я не могу уйти",
        "он сейчас меня бьет. Я не могу уйти",
    ),
)
def test_unrelated_post_action_negation_cannot_cancel_active_assault(text: str) -> None:
    assert assess_local_risk(text).level is RiskLevel.CRITICAL


@pytest.mark.parametrize(
    "text",
    (
        "он сейчас меня не бьет",
        "он сейчас мне не угрожает",
        "меня сегодня не выгнали",
        "я сейчас не причиню себе вред",
    ),
)
def test_true_predicate_negation_blocks_transactional_and_safety_actions(text: str) -> None:
    assert not _has_kind(text, HardSignalKind.VIOLENCE_OR_THREAT_NOW)
    assert not _has_kind(text, HardSignalKind.URGENT_SHELTER)
    assert not _has_kind(text, HardSignalKind.SUICIDE_OR_SELF_HARM)
    assert not _has_kind(text, HardSignalKind.CONCRETE_AID)


@pytest.mark.parametrize(
    "text",
    (
        "Можно поговорить с человеком?",
        "хочу поговорить с живым человеком",
        "позовите, пожалуйста, человека",
    ),
)
def test_explicit_human_grammar_routes_handoff(text: str) -> None:
    assert _has_kind(text, HardSignalKind.EXPLICIT_HUMAN_REQUEST)


@pytest.mark.parametrize(
    "text",
    (
        "мне не нужны продукты",
        "я не потеряла паспорт",
        "мне не нужен юрист",
        "меня сегодня не выгнали",
        "не хочу поговорить с психологом",
    ),
)
def test_negated_transactional_requests_do_not_authorize_actions(text: str) -> None:
    assert _decision(text) is PolicyEffect.NONE


@pytest.mark.parametrize(
    "draft",
    (
        "Я уже вызвала специалистку.",
        "Я передаю вашу заявку специалистке.",
        "Специалистка вам позвонит.",
        "Я сейчас отправлю заявку.",
        "Мы свяжемся завтра.",
    ),
)
def test_draft_guard_rejects_unconditional_operational_claims(draft: str) -> None:
    signals = extract_signals("мне хочется выговориться")
    decision = resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.COMPLETED,
            support=SupportDiagnostic(intent="open_conversation", draft_text=draft),
        )
    )

    assert decision.fallback_reason == "support_draft_guard"


@pytest.mark.asyncio
async def test_clear_crisis_returns_canonical_copy_when_diagnostic_preparation_fails() -> None:
    class FailingGateway:
        async def evaluate(self, context: AgentContext) -> AgentEvaluation:
            del context
            raise RuntimeError("preparation failed")

    service = ConversationService(store=InMemoryConversationStore(), gateway=FailingGateway())

    turn = await service.handle_text(incoming("я хочу умереть"))

    assert "8-800-2000-122" in turn.text
    assert [choice.id for choice in turn.choices] == ["continue_bot", "human"]


@pytest.mark.asyncio
async def test_clear_crisis_returns_canonical_copy_when_message_redaction_persistence_fails() -> None:
    class RedactionFailingStore(InMemoryConversationStore):
        async def append_message(
            self,
            record: object,
            role: str,
            content: str,
            audit: dict[str, object] | None = None,
        ) -> None:
            del record, role, content, audit
            raise RuntimeError("redaction persistence failure")

    gateway = FixedGateway()
    service = ConversationService(store=RedactionFailingStore(), gateway=gateway)

    turn = await service.handle_text(incoming("я хочу умереть", 341))

    assert "8-800-2000-122" in turn.text
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_database_failure_returns_truthful_retry_fallback_but_keeps_direct_crisis_copy() -> None:
    class FailingStore:
        async def ensure(self, incoming: IncomingMessage) -> object:
            del incoming
            raise RuntimeError("database unavailable")

    service = ConversationService(store=FailingStore(), gateway=FixedGateway())

    ordinary = await service.handle_text(incoming("мне плохо", 351))
    crisis = await service.handle_text(incoming("я хочу умереть", 352))

    assert "повторить" in ordinary.text.lower()
    assert any(choice.id == "human" for choice in ordinary.choices)
    assert "8-800-2000-122" in crisis.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    (
        ConversationState.DISCOVERING_NEED,
        ConversationState.CHOOSING_AID,
        ConversationState.COLLECTING_LOCATION,
        ConversationState.COLLECTING_CONTACT_METHOD,
        ConversationState.COLLECTING_CONTACT_VALUE,
        ConversationState.AID_REQUESTED,
        ConversationState.FOLLOWUP_WAITING,
        ConversationState.FOLLOWUP_SENT,
        ConversationState.FOLLOWUP_ANSWERED,
    ),
)
async def test_cancelled_finite_workflow_clears_every_abandoned_value(state: ConversationState) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway())
    record = await store.ensure(incoming(""))
    await store.update(
        record,
        state=state.value,
        need="legal",
        pending_aid_id="legal_consultation",
        pending_contact_method="other_telegram",
        pending_city="city",
        pending_district="district",
        pending_offer="psychologist",
    )

    await service.handle_text(incoming("отмена", 400 + list(ConversationState).index(state)))

    assert record.state == ConversationState.OPEN_CONVERSATION.value
    assert _workflow_values(record) == (None, None, None, None, None, None)


def _workflow_values(record: object) -> tuple[object, ...]:
    return tuple(
        getattr(record, field)
        for field in (
            "need",
            "pending_aid_id",
            "pending_contact_method",
            "pending_city",
            "pending_district",
            "pending_offer",
        )
    )


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

"""Round-4 regressions for systemic parser and workflow semantics.

The inputs are synthetic and every dependency remains local.  Provider calls
use the existing deterministic gateway double; no external service is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents import AgentCallResult, YandexAgentGateway
from app.domain import (
    ConversationState,
    DiagnosticStatus,
    IncomingMessage,
    PolicyContext,
    PolicyEffect,
    RiskLevel,
    SupportDiagnostic,
)
from app.policy import resolve_turn
from app.safety import assess_local_risk_from_signals
from app.service import ConversationService
from app.signals import extract_signals
from app.store import InMemoryConversationStore
from scripts import dialogue_eval

DATASET = Path("tests/fixtures/dialogue_scenarios.jsonl")


def _incoming(text: str, message_id: int) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=901,
        chat_id=902,
        text=text,
        message_id=message_id,
    )


def _gateway() -> YandexAgentGateway:
    async def call(name: str, _: str, __: str) -> AgentCallResult:
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


@pytest.mark.parametrize(
    "text",
    (
        "не хочу жить, в этом городе мне одиноко",
        "не хочу жить. С ним всё ужасно",
    ),
)
def test_new_clause_locative_or_relationship_text_cannot_suppress_suicide_intent(
    text: str,
) -> None:
    signals = extract_signals(text)

    assert assess_local_risk_from_signals(signals).level is RiskLevel.CRITICAL
    assert resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
        )
    ).effect is PolicyEffect.CRITICAL_ESCALATION


@pytest.mark.parametrize(
    "text",
    (
        "не хочу жить в этом городе",
        "не хочу жить с ним",
    ),
)
def test_same_clause_locative_or_relationship_complement_remains_a_near_miss(
    text: str,
) -> None:
    signals = extract_signals(text)

    assert assess_local_risk_from_signals(signals).level is RiskLevel.NONE


def test_dataset_v3_is_the_only_supported_clause_aware_schema(tmp_path: Path) -> None:
    row = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    fixture = tmp_path / "case.jsonl"
    row["version"] = 3
    fixture.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    assert dialogue_eval.load_cases(fixture)[0].version == 3

    row["version"] = 2
    fixture.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(dialogue_eval.DatasetError, match="unsupported version"):
        dialogue_eval.load_cases(fixture)


@pytest.mark.parametrize(
    ("state", "text"),
    (
        (ConversationState.COLLECTING_LOCATION, "не хочу город указывать"),
        (
            ConversationState.COLLECTING_CONTACT_VALUE,
            "номер телефона я давать не стану",
        ),
    ),
)
def test_state_bounded_refusal_grammar_escapes_before_value_capture(
    state: ConversationState,
    text: str,
) -> None:
    signals = extract_signals(text, state=state)
    decision = resolve_turn(
        PolicyContext(
            state=state.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            workflow_value=text,
        )
    )

    assert decision.effect is PolicyEffect.CANCEL_WORKFLOW


def test_refusal_grammar_does_not_bind_an_unrelated_negation_across_the_clause() -> None:
    text = "не хочу сейчас обсуждать погоду а номер телефона я дам"

    signals = extract_signals(text, state=ConversationState.COLLECTING_CONTACT_VALUE)

    assert not any(match.kind.value == "open_conversation_request" for match in signals.matches)


@pytest.mark.parametrize(
    "draft",
    (
        "С вами завтра свяжется специалистка.",
        "Я вам позвоню завтра.",
    ),
)
def test_draft_guard_blocks_definite_future_promises_across_word_order(
    draft: str,
) -> None:
    signals = extract_signals("мне тяжело")
    decision = resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.COMPLETED,
            support=SupportDiagnostic(
                intent="open_conversation",
                draft_text=draft,
            ),
        )
    )

    assert decision.fallback_reason == "support_draft_guard"


@pytest.mark.parametrize(
    "draft",
    (
        "Если захотите, специалистка сможет вам позвонить.",
        "С вами могла бы связаться специалистка.",
    ),
)
def test_draft_guard_allows_genuinely_conditional_or_modal_forms(draft: str) -> None:
    signals = extract_signals("мне тяжело")
    decision = resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.COMPLETED,
            support=SupportDiagnostic(
                intent="open_conversation",
                draft_text=draft,
            ),
        )
    )

    assert decision.fallback_reason is None
    assert decision.text == draft


@pytest.mark.asyncio
async def test_followup_same_renders_only_callbacks_valid_for_the_resulting_state() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(_incoming("", 910))
    await store.update(record, state=ConversationState.FOLLOWUP_SENT.value)

    turn = await service.handle_callback(_incoming("followup:same", 911), "followup:same")

    assert record.state == ConversationState.AID_REQUESTED.value
    assert {choice.id for choice in turn.choices} >= {"more_help", "finish"}
    next_turn = await service.handle_callback(_incoming("more_help", 912), "more_help")
    assert record.state == ConversationState.DISCOVERING_NEED.value
    assert any(choice.id.startswith("need:") for choice in next_turn.choices)


@pytest.mark.asyncio
async def test_level_two_later_uses_common_cleanup_and_cancels_reminders() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(_incoming("", 920))
    await store.update(
        record,
        state=ConversationState.FOLLOWUP_ANSWERED.value,
        need="legal",
        pending_aid_id="legal_consultation",
        pending_contact_method="phone",
        pending_city="synthetic-city",
    )
    request = await store.create_aid_request(
        record,
        "legal_consultation",
        "later",
        None,
        request_key="synthetic-level-two",
    )
    assert request.id

    await service.handle_callback(_incoming("level2:later", 921), "level2:later")

    assert record.state == ConversationState.OPEN_CONVERSATION.value
    assert record.need is None
    assert record.pending_aid_id is None
    assert record.pending_contact_method is None
    assert record.pending_city is None
    assert store.followup_jobs == []

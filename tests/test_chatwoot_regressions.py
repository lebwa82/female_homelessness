"""Connector-free regressions for the October 19 acceptance findings."""

from dataclasses import replace

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation

from app.chatwoot.service import ChatwootAgentService
from app.config import settings
from app.domain import (
    DiagnosticStatus,
    NeedKind,
    PolicyContext,
    PolicyEffect,
    RiskLevel,
    SafetyCategory,
    SafetyDiagnostic,
    SafetyEscalation,
    SupportDiagnostic,
    SupportIntent,
)
from app.policy import resolve_turn
from app.scenario import SCREENS
from scripts.chatwoot_regression_acceptance import check_reply_copy


def housing_evaluation(level=RiskLevel.NONE, categories=(), escalation=SafetyEscalation.NONE):
    return replace(ordinary_evaluation(),
        safety=SafetyDiagnostic(level=level, categories=categories, escalation=escalation),
        support=SupportDiagnostic(intent=SupportIntent.CONCRETE_NEED,
                                  need_hints=(NeedKind.HOUSING, NeedKind.SUPPORT),
                                  draft_text="Model draft is not the housing scenario."))


@pytest.mark.parametrize("level,categories,escalation", [
    (RiskLevel.NONE, (), SafetyEscalation.NONE),
    (RiskLevel.CONCERN, (SafetyCategory.ACUTE_HOMELESSNESS,), SafetyEscalation.NONE),
    (RiskLevel.CONCERN, (SafetyCategory.ACUTE_HOMELESSNESS,), SafetyEscalation.HANDOFF),
    (RiskLevel.URGENT, (SafetyCategory.ACUTE_HOMELESSNESS,), SafetyEscalation.HANDOFF),
])
async def test_housing_text_opens_the_same_scenario_as_the_button(monkeypatch, level, categories, escalation):
    monkeypatch.setattr(settings, "llm_enabled", True)
    api = FakeChatwoot()
    gateway = StubGateway(housing_evaluation(level, categories, escalation))
    service = ChatwootAgentService(api, gateway=gateway, duty_team_id=9)
    assert await service.process(event("мне некуда пойти помогите"))
    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "s21"
    assert api.replies[-1]["text"] == SCREENS["s21"]["texts"][0]
    assert any(c.id == "aid:hostel_3_nights" for c in api.replies[-1]["choices"])
    assert api.teams == []
    assert not api.conversation["custom_attributes"].get("handoff_requested")
    assert gateway.calls == 1


async def test_bot_owned_housing_dialogue_can_leave_the_previous_false_safety_route(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(
        workflow_state="safety_escalation", workflow_need="housing",
        scenario={"screen": "s11", "variant": 0}, handoff_requested=True,
    )
    service = ChatwootAgentService(api, gateway=StubGateway(housing_evaluation()), duty_team_id=9)
    assert await service.process(event("мне некуда пойти помогите"))
    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "s21"
    assert api.replies[-1]["text"] == SCREENS["s21"]["texts"][0]
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"


@pytest.mark.parametrize("level,categories,escalation", [
    (RiskLevel.CRITICAL, (SafetyCategory.ACUTE_HOMELESSNESS,), SafetyEscalation.HANDOFF),
    (RiskLevel.URGENT, (SafetyCategory.ACUTE_HOMELESSNESS, SafetyCategory.VIOLENCE_THREAT),
     SafetyEscalation.HANDOFF),
    (RiskLevel.CONCERN, (SafetyCategory.CHILD_SAFETY,), SafetyEscalation.HANDOFF),
    (RiskLevel.CONCERN, (), SafetyEscalation.SUICIDE),
])
def test_housing_request_does_not_override_independent_safety_signals(level, categories, escalation):
    evaluation = housing_evaluation(level, categories, escalation)
    decision = resolve_turn(PolicyContext(
        state="open_conversation", safety_status=evaluation.safety_status,
        support_status=evaluation.support_status, safety=evaluation.safety, support=evaluation.support,
    ))
    assert decision.effect is PolicyEffect.SAFETY_ESCALATION


def test_housing_request_does_not_override_explicit_human_choice_or_missing_safety():
    evaluation = housing_evaluation()
    context = PolicyContext(state="open_conversation", safety_status=DiagnosticStatus.COMPLETED,
                            support_status=DiagnosticStatus.COMPLETED,
                            safety=evaluation.safety, support=evaluation.support)
    human = context.support.model_copy(update={"intent": SupportIntent.EXPLICIT_HUMAN_REQUEST})
    assert resolve_turn(context.model_copy(update={"support": human})).effect is PolicyEffect.HUMAN_HANDOFF
    missing = resolve_turn(context.model_copy(update={"safety_status": DiagnosticStatus.UNAVAILABLE}))
    assert missing.fallback_reason == "safety_diagnostic_unavailable"


@pytest.mark.parametrize("text", [
    "Здравствуйте!\nЗдравствуйте. Я рядом.", "Я рядом и готовы поговорить.",
    "Расскажите, где вы сейчас находитесь?", "В каком городе вы находитесь?",
])
def test_live_copy_gate_rejects_reported_defects(text):
    with pytest.raises(AssertionError):
        check_reply_copy(text)


def test_live_copy_gate_accepts_neutral_singular_reply():
    check_reply_copy("Здравствуйте. Я рядом и готова поговорить. Что сейчас важно?")


def test_live_copy_gate_accepts_canonical_assurance_that_location_is_not_required():
    check_reply_copy(SCREENS["s33"]["texts"][0])

import pytest

from app.domain import (
    ChoiceSet,
    PolicyEffect,
    RiskAssessment,
    RiskLevel,
    SupportPlan,
)
from app.policy import resolve_turn


def safe_risk() -> RiskAssessment:
    return RiskAssessment(level=RiskLevel.NONE, detector="test")


def critical_suicide_risk() -> RiskAssessment:
    return RiskAssessment(level=RiskLevel.CRITICAL, categories=("suicide",), detector="test")


def aid_plan() -> SupportPlan:
    return SupportPlan(
        intent="aid_interest",
        next_action="offer_aid",
        text="Давайте подберём варианты.",
        need="food_money",
    )


@pytest.mark.parametrize(
    ("text", "intent", "action"),
    [
        ("мне хочется выговориться", "open_conversation", "continue_conversation"),
        ("можешь меня выслушать?", "open_conversation", "continue_conversation"),
        ("мне плохо", "open_conversation", "continue_conversation"),
    ],
)
def test_open_conversation_never_gets_need_menu(text: str, intent: str, action: str) -> None:
    decision = resolve_turn(
        safe_risk(),
        SupportPlan(intent=intent, next_action=action, text=text, choice_set="need_categories"),
        "open_conversation",
    )

    assert decision.choice_set is ChoiceSet.NONE
    assert decision.effect is PolicyEffect.NONE


def test_explicit_human_request_is_not_a_risk_but_becomes_handoff() -> None:
    decision = resolve_turn(
        safe_risk(),
        SupportPlan(
            intent="explicit_human_request",
            next_action="request_human",
            text="Позову человека.",
        ),
        "open_conversation",
    )

    assert decision.effect is PolicyEffect.HUMAN_HANDOFF


def test_critical_risk_discards_support_plan() -> None:
    decision = resolve_turn(critical_suicide_risk(), aid_plan(), "open_conversation")

    assert decision.effect is PolicyEffect.CRITICAL_ESCALATION
    assert "8-800-2000-122" in decision.text


def test_unknown_risk_blocks_a_valid_support_plan() -> None:
    decision = resolve_turn(
        RiskAssessment(level=RiskLevel.UNKNOWN, detector="test"), aid_plan(), "open_conversation"
    )

    assert decision.effect is PolicyEffect.NONE
    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE
    assert decision.fallback_reason == "risk_unknown"


def test_missing_plan_falls_back_to_open_conversation_without_a_menu() -> None:
    decision = resolve_turn(safe_risk(), None, "open_conversation")

    assert decision.effect is PolicyEffect.NONE
    assert decision.choice_set is ChoiceSet.NONE
    assert decision.fallback_reason == "support_plan_missing"


@pytest.mark.parametrize(
    ("intent", "next_action"),
    [
        ("explicit_human_request", "continue_conversation"),
        ("psychologist_request", "clarify"),
    ],
)
def test_inconsistent_side_effect_plan_has_no_effect(
    intent: str, next_action: str
) -> None:
    decision = resolve_turn(
        safe_risk(),
        SupportPlan(intent=intent, next_action=next_action, text="Небезопасное обещание."),
        "open_conversation",
    )

    assert decision.effect is PolicyEffect.NONE
    assert decision.choice_set is ChoiceSet.NONE
    assert decision.fallback_reason == "inconsistent_plan"


def test_psychologist_considering_offers_only_confirmed_interest_choice_set() -> None:
    decision = resolve_turn(
        safe_risk(),
        SupportPlan(
            intent="psychologist_considering",
            next_action="clarify",
            text="Могу рассказать о поддержке психолога.",
        ),
        "open_conversation",
    )

    assert decision.effect is PolicyEffect.NONE
    assert decision.choice_set is ChoiceSet.PSYCHOLOGIST_INTEREST


@pytest.mark.parametrize("level", [RiskLevel.CONCERN, RiskLevel.URGENT])
def test_noncritical_risk_preserves_valid_conversation_plan(level: RiskLevel) -> None:
    decision = resolve_turn(
        RiskAssessment(level=level, detector="test"),
        SupportPlan(
            intent="open_conversation",
            next_action="continue_conversation",
            text="Я рядом.",
        ),
        "open_conversation",
    )

    assert decision.text == "Я рядом."
    assert decision.effect is PolicyEffect.NONE
    assert decision.fallback_reason is None

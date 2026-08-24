import json
from pathlib import Path

import pytest

from app.domain import (
    ChoiceSet,
    ConversationState,
    DiagnosticStatus,
    PolicyContext,
    PolicyEffect,
    RiskAssessment,
    RiskLevel,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportOffer,
)
from app.policy import resolve_turn
from app.safety import assess_local_risk_from_signals
from app.signals import extract_signals

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dialogue_scenarios.jsonl"
_PENDING_PSYCHOLOGIST_CASE_IDS = frozenset(
    {
        "psychologist-considering-01",
        "psychologist-02",
        "psychologist-03",
        "psychologist-04",
        "psychologist-05",
    }
)


def context(
    text: str,
    *,
    state: str = ConversationState.OPEN_CONVERSATION.value,
    pending_offer: SupportOffer | None = None,
    support: SupportDiagnostic | None = None,
    support_status: DiagnosticStatus = DiagnosticStatus.COMPLETED,
    local_risk: RiskAssessment | None = None,
) -> PolicyContext:
    signals = extract_signals(text, pending_offer=pending_offer)
    return PolicyContext(
        state=state,
        signals=signals,
        local_risk=local_risk or assess_local_risk_from_signals(signals),
        safety_status=DiagnosticStatus.INVALID,
        support_status=support_status,
        safety=None,
        support=support,
        pending_offer=pending_offer,
        workflow_value=text,
    )


@pytest.mark.parametrize(
    ("text", "state", "expected_effect", "expected_choices"),
    (
        ("не хочу жить", ConversationState.COLLECTING_CONTACT_METHOD.value, PolicyEffect.CRITICAL_ESCALATION, ChoiceSet.SAFE_CONTINUE),
        ("Позовите человека", ConversationState.COLLECTING_CONTACT_METHOD.value, PolicyEffect.HUMAN_HANDOFF, ChoiceSet.SAFE_CONTINUE),
        ("город Москва", ConversationState.COLLECTING_LOCATION.value, PolicyEffect.CAPTURE_LOCATION, ChoiceSet.CONTACT_METHODS),
        ("хочу поговорить с психологом", ConversationState.OPEN_CONVERSATION.value, PolicyEffect.START_PSYCHOLOGIST_REQUEST, ChoiceSet.CONTACT_METHODS),
        ("мне нужны продукты", ConversationState.OPEN_CONVERSATION.value, PolicyEffect.NONE, ChoiceSet.CONTEXTUAL_NEEDS),
        ("какую помощь можно получить", ConversationState.OPEN_CONVERSATION.value, PolicyEffect.START_NEED_DISCOVERY, ChoiceSet.NEED_CATEGORIES),
        ("мне хочется выговориться", ConversationState.OPEN_CONVERSATION.value, PolicyEffect.NONE, ChoiceSet.NONE),
    ),
)
def test_policy_truth_table_has_the_required_deterministic_precedence(
    text: str, state: str, expected_effect: PolicyEffect, expected_choices: ChoiceSet
) -> None:
    decision = resolve_turn(context(text, state=state, support=SupportDiagnostic(intent="aid_interest", draft_text="Я рядом.")))

    assert decision.effect is expected_effect
    assert decision.choice_set is expected_choices


def test_critical_wins_over_human_even_when_diagnostics_are_wrong_but_valid() -> None:
    text = "Позовите человека, но я не хочу жить"
    local_risk = RiskAssessment(level=RiskLevel.CRITICAL, categories=("suicide",), detector="test")
    baseline = resolve_turn(context(text, local_risk=local_risk))
    mutated = resolve_turn(
        context(
            text,
            local_risk=local_risk,
            support=SupportDiagnostic(
                intent="concrete_need",
                need_hint="legal",
                draft_text="Я оформила заявку и передала контакт.",
            ),
        ).model_copy(
            update={
                "safety_status": DiagnosticStatus.COMPLETED,
                "safety": SafetyDiagnostic(level="none", confidence=0.99, rationale="wrong"),
            }
        )
    )

    assert baseline.effect is PolicyEffect.CRITICAL_ESCALATION
    assert baseline.text == mutated.text
    assert baseline.choice_set is mutated.choice_set
    assert baseline.side_effects == mutated.side_effects


def test_explicit_human_request_survives_invalid_diagnostics_and_finite_workflow() -> None:
    decision = resolve_turn(
        context(
            "Позовите человека",
            state=ConversationState.COLLECTING_CONTACT_VALUE.value,
            support_status=DiagnosticStatus.UNAVAILABLE,
        )
    )

    assert decision.effect is PolicyEffect.HUMAN_HANDOFF


def test_local_inspection_failure_is_distinct_from_provider_failure() -> None:
    decision = resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=None,
            local_risk=RiskAssessment(level=RiskLevel.UNKNOWN, detector="local-signals"),
            safety_status=DiagnosticStatus.UNAVAILABLE,
            support_status=DiagnosticStatus.UNAVAILABLE,
        )
    )

    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE
    assert decision.fallback_reason == "local_input_unavailable"


@pytest.mark.parametrize(
    "text",
    (
        "не хочу жить",
        "Позовите человека",
        "хочу поговорить с психологом",
        "мне нужны продукты",
        "какую помощь можно получить",
    ),
)
def test_wrong_or_missing_diagnostics_do_not_mutate_hard_local_projection(text: str) -> None:
    baseline = resolve_turn(context(text, support_status=DiagnosticStatus.UNAVAILABLE))
    mutated = resolve_turn(
        context(
            text,
            support=SupportDiagnostic(
                intent="explicit_human_request",
                need_hint="legal",
                draft_text="Я оформила заявку и передала контакт.",
            ),
        ).model_copy(
            update={
                "safety_status": DiagnosticStatus.COMPLETED,
                "safety": SafetyDiagnostic(level="none", confidence=0.99, rationale="wrong"),
            }
        )
    )

    assert (baseline.effect, baseline.choice_set, baseline.side_effects) == (
        mutated.effect,
        mutated.choice_set,
        mutated.side_effects,
    )


def test_pending_offer_requires_a_verified_followup_before_a_psychologist_button() -> None:
    offer_turn = resolve_turn(
        context(
            "мне трудно",
            support=SupportDiagnostic(
                intent="psychologist_considering",
                draft_text="Я рядом. Могу рассказать о психологе.",
                suggested_support=SupportOffer.PSYCHOLOGIST,
            ),
        )
    )
    interest_turn = resolve_turn(
        context("расскажите, пожалуйста", pending_offer=SupportOffer.PSYCHOLOGIST)
    )

    assert offer_turn.choice_set is ChoiceSet.NONE
    assert offer_turn.offered_support is SupportOffer.PSYCHOLOGIST
    assert interest_turn.choice_set is ChoiceSet.PSYCHOLOGIST_INTEREST


@pytest.mark.parametrize(
    "draft",
    (
        "",
        "Я оформила заявку и передала контакт.",
        "Ваша заявка уже принята, с вами свяжутся.",
        "Оператор уже подключён, ваша заявка зарегистрирована и данные отправлены.",
        "С оператором уже связались, запрос зарегистрировали, информацию отправили.",
        "Заявка зарегистрирована, если будут вопросы — напишите.",
        "Ваша заявка на помощь уже была зарегистрирована.",
        "Запрос на помощь уже успешно зарегистрирован.",
        "Контакт для заявки уже был передан специалисту.",
    ),
)
def test_open_conversation_draft_guard_rejects_empty_or_external_action_claims(draft: str) -> None:
    if draft:
        support = SupportDiagnostic(intent="open_conversation", draft_text=draft)
        status = DiagnosticStatus.COMPLETED
    else:
        support = None
        status = DiagnosticStatus.INVALID
    decision = resolve_turn(context("мне хочется выговориться", support=support, support_status=status))

    assert decision.choice_set is ChoiceSet.NONE
    assert decision.text != draft
    assert decision.fallback_reason in {"support_diagnostic_unavailable", "support_draft_guard"}


@pytest.mark.parametrize(
    "draft",
    (
        "Оператор может подключиться, если вы захотите позвать человека.",
        "Я могу объяснить, какие данные обычно нужны для заявки.",
        "Если захотите, можно передать контакт позже.",
        "Если с вами свяжутся, можно будет уточнить условия.",
        "Заявка может быть зарегистрирована после вашего согласия.",
    ),
)
def test_draft_guard_allows_informational_or_future_language_without_completion_claim(draft: str) -> None:
    decision = resolve_turn(
        context(
            "мне хочется выговориться",
            support=SupportDiagnostic(intent="open_conversation", draft_text=draft),
        )
    )

    assert decision.text == draft
    assert decision.fallback_reason is None


def test_concern_narrative_without_request_has_no_aid_menu_but_records_safety() -> None:
    decision = resolve_turn(context("я боюсь возвращаться домой", support=SupportDiagnostic(intent="aid_interest", draft_text="Я рядом.")))

    assert decision.effect is PolicyEffect.NONE
    assert decision.choice_set is ChoiceSet.NONE
    assert [effect.value for effect in decision.side_effects] == ["record_safety"]


def test_all_versioned_final_user_turns_have_a_deterministic_route_and_open_rows_stay_open() -> None:
    rows = [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 65

    for row in rows:
        history = tuple((str(role), str(text)) for role, text in row["history"])
        final_text = next(text for role, text in reversed(history) if role == "user")
        pending_offer = (
            SupportOffer.PSYCHOLOGIST
            if row["id"] in _PENDING_PSYCHOLOGIST_CASE_IDS
            else None
        )
        signals = extract_signals(final_text, pending_offer=pending_offer)
        decision = resolve_turn(
            PolicyContext(
                state=ConversationState.OPEN_CONVERSATION.value,
                signals=signals,
                local_risk=assess_local_risk_from_signals(signals),
                support_status=DiagnosticStatus.COMPLETED,
                support=SupportDiagnostic(intent="open_conversation", draft_text="Я рядом."),
                pending_offer=pending_offer,
                workflow_value=final_text,
            )
        )

        assert decision.text
        if row["group"] in {"open_conversation", "human_near_miss"}:
            assert decision.choice_set is ChoiceSet.NONE
            assert decision.effect is PolicyEffect.NONE

from app.domain import (
    ChoiceSet,
    ConversationState,
    DiagnosticStatus,
    NeedKind,
    PolicyContext,
    PolicyEffect,
    PolicySideEffect,
    RiskLevel,
    SafetyCategory,
    SafetyDiagnostic,
    SafetyEscalation,
    SupportDiagnostic,
    SupportIntent,
    SupportOffer,
)
from app.policy import resolve_turn
from app.ui import choices_for


def context(
    *,
    state: ConversationState = ConversationState.OPEN_CONVERSATION,
    safety: SafetyDiagnostic | None = None,
    support: SupportDiagnostic | None = None,
    safety_status: DiagnosticStatus = DiagnosticStatus.COMPLETED,
    support_status: DiagnosticStatus = DiagnosticStatus.COMPLETED,
    pending_offer: SupportOffer | None = None,
) -> PolicyContext:
    return PolicyContext(
        state=state.value,
        safety_status=safety_status,
        support_status=support_status,
        safety=safety or SafetyDiagnostic(level=RiskLevel.NONE),
        support=support or SupportDiagnostic(intent=SupportIntent.OPEN_CONVERSATION, draft_text="Я рядом."),
        pending_offer=pending_offer,
    )


def test_model_critical_diagnostic_is_authoritative_over_empty_local_projection() -> None:
    """A provider-confirmed crisis must not depend on a backend phrase list."""
    decision = resolve_turn(
        context(safety=SafetyDiagnostic(level=RiskLevel.CRITICAL, categories=("suicide",)))
    )

    assert decision.effect is PolicyEffect.SAFETY_ESCALATION
    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE
    assert "8-800-2000-122" in decision.text


def test_model_handoff_route_is_authoritative_even_when_urgency_is_only_concern() -> None:
    """Child-safety fears use S11; urgency is not the product-routing decision."""
    decision = resolve_turn(
        context(
            safety=SafetyDiagnostic(
                level=RiskLevel.CONCERN,
                escalation=SafetyEscalation.HANDOFF,
                categories=(SafetyCategory.CHILD_SAFETY,),
            ),
            support=SupportDiagnostic(
                intent=SupportIntent.CONCRETE_NEED,
                need_hints=(NeedKind.CHILDREN, NeedKind.LEGAL, NeedKind.SUPPORT),
                draft_text="Слышу, как тревожно сейчас.",
            ),
        )
    )

    assert decision.effect is PolicyEffect.SAFETY_ESCALATION
    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE
    assert decision.need is NeedKind.CHILDREN
    assert "8-800-2000-122" not in decision.text


def test_model_suicide_route_uses_s12_without_relying_on_a_category_string() -> None:
    decision = resolve_turn(
        context(
            safety=SafetyDiagnostic(
                level=RiskLevel.CONCERN,
                escalation=SafetyEscalation.SUICIDE,
            )
        )
    )

    assert decision.effect is PolicyEffect.SAFETY_ESCALATION
    assert "8-800-2000-122" in decision.text


def test_confirmed_s11_route_does_not_depend_on_the_parallel_support_call() -> None:
    decision = resolve_turn(
        context(
            safety=SafetyDiagnostic(
                level=RiskLevel.CONCERN,
                escalation=SafetyEscalation.HANDOFF,
                categories=(SafetyCategory.CHILD_SAFETY,),
            ),
            support=None,
            support_status=DiagnosticStatus.UNAVAILABLE,
        )
    )

    assert decision.effect is PolicyEffect.SAFETY_ESCALATION
    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE


def test_unavailable_safety_diagnostic_cannot_open_the_regular_need_flow() -> None:
    decision = resolve_turn(
        context(
            safety=None,
            safety_status=DiagnosticStatus.UNAVAILABLE,
            support=SupportDiagnostic(
                intent=SupportIntent.CONCRETE_NEED,
                need_hints=(NeedKind.FOOD_MONEY,),
                draft_text="Можно показать помощь с едой.",
            ),
        )
    )

    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE
    assert decision.fallback_reason == "safety_diagnostic_unavailable"


def test_model_human_request_starts_handoff() -> None:
    decision = resolve_turn(
        context(
            support=SupportDiagnostic(
                intent=SupportIntent.EXPLICIT_HUMAN_REQUEST,
                draft_text="Я рядом.",
            )
        )
    )

    assert decision.effect is PolicyEffect.HUMAN_HANDOFF
    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE


def test_direct_human_safety_category_is_a_handoff_not_a_safety_event() -> None:
    decision = resolve_turn(
        context(
            safety=SafetyDiagnostic(
                level=RiskLevel.CONCERN,
                escalation=SafetyEscalation.HANDOFF,
                categories=(SafetyCategory.DIRECT_HUMAN_REQUEST,),
            ),
            support=None,
            support_status=DiagnosticStatus.UNAVAILABLE,
        )
    )

    assert decision.effect is PolicyEffect.HUMAN_HANDOFF
    assert decision.side_effects == ()


def test_model_need_hints_render_every_relevant_button_and_human() -> None:
    decision = resolve_turn(
        context(
            support=SupportDiagnostic(
                intent=SupportIntent.CONCRETE_NEED,
                need_hints=(NeedKind.LEGAL, NeedKind.HOUSING, NeedKind.LEGAL),
                draft_text="С потерей документов правда бывает очень тяжело.",
            )
        )
    )
    choices = choices_for(
        decision.choice_set,
        decision.catalog_item_ids,
        contextual_needs=decision.contextual_needs,
    )

    assert decision.choice_set is ChoiceSet.CONTEXTUAL_NEEDS
    assert [choice.id for choice in choices] == ["need:legal", "need:housing", "human"]


def test_unavailable_support_model_does_not_use_a_local_text_fallback() -> None:
    decision = resolve_turn(
        context(
            safety_status=DiagnosticStatus.UNAVAILABLE,
            support_status=DiagnosticStatus.UNAVAILABLE,
            safety=None,
            support=None,
        )
    )

    assert decision.choice_set is ChoiceSet.SAFE_CONTINUE
    assert decision.fallback_reason == "safety_diagnostic_unavailable"


def test_model_concern_is_logged_without_forcing_a_help_menu() -> None:
    decision = resolve_turn(
        context(
            safety=SafetyDiagnostic(
                level=RiskLevel.CONCERN,
                categories=(SafetyCategory.VIOLENCE_THREAT,),
            ),
            support=SupportDiagnostic(intent=SupportIntent.OPEN_CONVERSATION, draft_text="Я рядом."),
        )
    )

    assert decision.effect is PolicyEffect.NONE
    assert decision.choice_set is ChoiceSet.NONE
    assert decision.side_effects == (PolicySideEffect.RECORD_SAFETY,)


def test_callback_created_contact_workflow_stays_deterministic() -> None:
    decision = resolve_turn(
        context(
            state=ConversationState.COLLECTING_CONTACT_VALUE,
            support=SupportDiagnostic(
                intent=SupportIntent.OPEN_CONVERSATION,
                need_hints=(NeedKind.LEGAL,),
                draft_text="Я рядом.",
            ),
        )
    )

    assert decision.effect is PolicyEffect.COMPLETE_CONTACT
    assert decision.choice_set is ChoiceSet.MORE_HELP


def test_pending_psychologist_offer_uses_model_intent() -> None:
    decision = resolve_turn(
        context(
            pending_offer=SupportOffer.PSYCHOLOGIST,
            support=SupportDiagnostic(
                intent=SupportIntent.PSYCHOLOGIST_CONSIDERING,
                draft_text="Расскажу подробнее.",
            ),
        )
    )

    assert decision.choice_set is ChoiceSet.PSYCHOLOGIST_INTEREST

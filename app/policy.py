from __future__ import annotations

from app.domain import (
    ChoiceSet,
    PolicyEffect,
    ResolvedTurn,
    RiskAssessment,
    RiskLevel,
    SupportAction,
    SupportIntent,
    SupportPlan,
)

_SAFE_FALLBACK = "Я рядом и готова продолжить. Можно написать, что сейчас важно."


def resolve_turn(
    risk: RiskAssessment,
    plan: SupportPlan | None,
    state: str,
) -> ResolvedTurn:
    """Resolve model outputs to the only permitted UI and workflow decision."""
    if risk.level is RiskLevel.CRITICAL:
        return critical_resolved_turn(risk)
    if risk.level is RiskLevel.UNKNOWN:
        return ResolvedTurn(
            text="Я здесь. Можно продолжить разговор или позвать человека.",
            choice_set=ChoiceSet.SAFE_CONTINUE,
            fallback_reason="risk_unknown",
        )
    if plan is None:
        return ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="support_plan_missing")
    if not _is_consistent(plan, state):
        return ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="inconsistent_plan")

    if plan.intent is SupportIntent.EXPLICIT_HUMAN_REQUEST:
        return ResolvedTurn(text=plan.text, effect=PolicyEffect.HUMAN_HANDOFF)
    if plan.intent is SupportIntent.OPEN_CONVERSATION:
        return ResolvedTurn(text=plan.text, offered_support=plan.offered_support)
    if plan.intent is SupportIntent.PSYCHOLOGIST_CONSIDERING:
        return ResolvedTurn(text=plan.text, choice_set=ChoiceSet.PSYCHOLOGIST_INTEREST)
    if plan.intent is SupportIntent.PSYCHOLOGIST_REQUEST:
        return ResolvedTurn(text=plan.text, effect=PolicyEffect.START_PSYCHOLOGIST_REQUEST)
    if plan.next_action is SupportAction.OFFER_AID:
        return ResolvedTurn(
            text=plan.text,
            effect=PolicyEffect.OFFER_AID,
            need=plan.need,
            catalog_item_ids=plan.catalog_item_ids,
        )
    if plan.next_action is SupportAction.CLOSE:
        return ResolvedTurn(text=plan.text, effect=PolicyEffect.CLOSE)
    return ResolvedTurn(text=plan.text, offered_support=plan.offered_support)


def critical_resolved_turn(risk: RiskAssessment) -> ResolvedTurn:
    if "suicide" in risk.categories:
        text = (
            "Слышу вас. Это важно.\n\n"
            "Телефон доверия — бесплатно, круглосуточно: 8-800-2000-122\n\n"
            "Я здесь параллельно. Можно написать, что происходит."
        )
    else:
        text = (
            "Слышу вас. Хочу убедиться, что вы сейчас в безопасности. "
            "Если есть непосредственная опасность и это безопасно, можно позвонить 112. "
            "Зову человека, а здесь можно продолжать писать."
        )
    return ResolvedTurn(
        text=text,
        choice_set=ChoiceSet.SAFE_CONTINUE,
        effect=PolicyEffect.CRITICAL_ESCALATION,
    )


def _is_consistent(plan: SupportPlan, state: str) -> bool:
    del state
    expected_actions = {
        SupportIntent.OPEN_CONVERSATION: (SupportAction.CONTINUE_CONVERSATION,),
        SupportIntent.CONCRETE_NEED: (SupportAction.OFFER_AID,),
        SupportIntent.AID_INTEREST: (SupportAction.OFFER_AID,),
        SupportIntent.PSYCHOLOGIST_CONSIDERING: (SupportAction.CLARIFY,),
        SupportIntent.PSYCHOLOGIST_REQUEST: (SupportAction.START_PSYCHOLOGIST_REQUEST,),
        SupportIntent.VERIFIED_INFORMATION: (SupportAction.PROVIDE_VERIFIED_INFO,),
        SupportIntent.EXPLICIT_HUMAN_REQUEST: (SupportAction.REQUEST_HUMAN,),
        SupportIntent.CLOSE: (SupportAction.CLOSE,),
    }
    if plan.next_action not in expected_actions[plan.intent]:
        return False
    if plan.next_action is SupportAction.OFFER_AID:
        return plan.need is not None
    return True

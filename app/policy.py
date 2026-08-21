from __future__ import annotations

from app.catalog import available_aid_for_need, get_aid_item
from app.domain import (
    ChoiceSet,
    ConversationState,
    NeedKind,
    PolicyEffect,
    PolicySideEffect,
    ResolvedTurn,
    RiskAssessment,
    RiskLevel,
    SupportAction,
    SupportIntent,
    SupportPlan,
)

_SAFE_FALLBACK = "Я рядом и готова продолжить. Можно написать, что сейчас важно."
_UNKNOWN_PROMPT = "Я здесь. Можно продолжить разговор или позвать человека."
HUMAN_HANDOFF_PROMPT = (
    "Слышу вас. Зову человека, который работает с этим ресурсом. Здесь можно продолжать писать."
)
CONTACT_PROMPT = "Чтобы это передать, нужен удобный способ связи. Что вам подходит?"
MORE_HELP_PROMPT = "Хорошо, запрос сохранён. Нужно что-то ещё?"
_FINITE_WORKFLOW_STATES = frozenset(
    {
        ConversationState.CHOOSING_AID.value,
        ConversationState.COLLECTING_LOCATION.value,
        ConversationState.COLLECTING_CONTACT_METHOD.value,
        ConversationState.COLLECTING_CONTACT_VALUE.value,
        ConversationState.AID_REQUESTED.value,
        ConversationState.FOLLOWUP_WAITING.value,
        ConversationState.FOLLOWUP_SENT.value,
        ConversationState.FOLLOWUP_ANSWERED.value,
    }
)


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
            text=_UNKNOWN_PROMPT,
            choice_set=ChoiceSet.SAFE_CONTINUE,
            fallback_reason="risk_unknown",
        )
    if plan is None:
        return _with_safety_side_effect(
            risk,
            ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="support_plan_missing"),
        )
    if not _is_consistent(plan):
        return _with_safety_side_effect(
            risk,
            ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="inconsistent_plan"),
        )
    if _starts_new_workflow(plan) and state in _FINITE_WORKFLOW_STATES:
        return _with_safety_side_effect(
            risk,
            ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="workflow_active"),
        )

    if plan.intent is SupportIntent.EXPLICIT_HUMAN_REQUEST:
        decision = ResolvedTurn(
            text=HUMAN_HANDOFF_PROMPT,
            choice_set=ChoiceSet.SAFE_CONTINUE,
            effect=PolicyEffect.HUMAN_HANDOFF,
        )
    elif plan.intent is SupportIntent.OPEN_CONVERSATION:
        decision = ResolvedTurn(text=plan.text, offered_support=plan.offered_support)
    elif plan.intent is SupportIntent.PSYCHOLOGIST_CONSIDERING:
        decision = ResolvedTurn(text=plan.text, choice_set=ChoiceSet.PSYCHOLOGIST_INTEREST)
    elif plan.intent is SupportIntent.PSYCHOLOGIST_REQUEST:
        decision = ResolvedTurn(
            text=plan.text,
            choice_set=ChoiceSet.CONTACT_METHODS,
            effect=PolicyEffect.START_PSYCHOLOGIST_REQUEST,
        )
    elif plan.next_action is SupportAction.OFFER_AID:
        item_ids = _catalog_item_ids(plan.need, plan.catalog_item_ids)
        if not item_ids:
            decision = ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="catalog_items_missing")
        else:
            decision = ResolvedTurn(
                text=_aid_offer_text(plan.text, item_ids),
                choice_set=ChoiceSet.AID_CATALOG,
                effect=PolicyEffect.OFFER_AID,
                need=plan.need,
                catalog_item_ids=item_ids,
            )
    elif plan.next_action is SupportAction.CLOSE:
        decision = ResolvedTurn(text=plan.text, effect=PolicyEffect.CLOSE)
    else:
        decision = ResolvedTurn(text=plan.text, offered_support=plan.offered_support)
    return _with_safety_side_effect(risk, decision)


def resolve_workflow_turn(
    risk: RiskAssessment,
    state: str,
    workflow_value: str,
    need: str | None,
) -> ResolvedTurn:
    """Normalize a deterministic finite-workflow text input before execution."""
    if state == ConversationState.COLLECTING_LOCATION.value:
        decision = ResolvedTurn(
            text=CONTACT_PROMPT,
            choice_set=ChoiceSet.CONTACT_METHODS,
            effect=PolicyEffect.CAPTURE_LOCATION,
            workflow_value=workflow_value[:120],
        )
    elif state == ConversationState.COLLECTING_CONTACT_VALUE.value:
        decision = ResolvedTurn(
            text=MORE_HELP_PROMPT,
            choice_set=ChoiceSet.MORE_HELP,
            effect=PolicyEffect.COMPLETE_CONTACT,
            workflow_value=workflow_value.strip()[:320],
        )
    elif state == ConversationState.CHOOSING_AID.value:
        try:
            item_ids = _catalog_item_ids(NeedKind(need or ""), ())
        except ValueError:
            item_ids = ()
        decision = ResolvedTurn(
            text=_aid_offer_text("", item_ids) if item_ids else _SAFE_FALLBACK,
            choice_set=ChoiceSet.AID_CATALOG if item_ids else ChoiceSet.NONE,
            effect=PolicyEffect.REPLAY_WORKFLOW,
            catalog_item_ids=item_ids,
            fallback_reason=None if item_ids else "workflow_invalid",
        )
    elif state == ConversationState.COLLECTING_CONTACT_METHOD.value:
        decision = ResolvedTurn(
            text=CONTACT_PROMPT,
            choice_set=ChoiceSet.CONTACT_METHODS,
            effect=PolicyEffect.REPLAY_WORKFLOW,
        )
    elif state == ConversationState.AID_REQUESTED.value:
        decision = ResolvedTurn(
            text=MORE_HELP_PROMPT,
            choice_set=ChoiceSet.MORE_HELP,
            effect=PolicyEffect.REPLAY_WORKFLOW,
        )
    else:
        decision = ResolvedTurn(
            text=_SAFE_FALLBACK,
            effect=PolicyEffect.REPLAY_WORKFLOW,
            fallback_reason="workflow_active",
        )
    return _with_safety_side_effect(risk, decision)


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
        side_effects=(PolicySideEffect.RECORD_SAFETY,),
    )


def _catalog_item_ids(need: NeedKind | None, requested: tuple[str, ...]) -> tuple[str, ...]:
    if need is None:
        return ()
    allowed = tuple(item.id for item in available_aid_for_need(need))
    requested_ids = requested or allowed
    return tuple(item_id for item_id in requested_ids if item_id in allowed and get_aid_item(item_id))


def _aid_offer_text(prefix: str, item_ids: tuple[str, ...]) -> str:
    descriptions = "\n".join(
        f"— {item.label}" for item_id in item_ids if (item := get_aid_item(item_id)) is not None
    )
    lead = f"{prefix.strip()}\n\n" if prefix.strip() else ""
    return f"{lead}Вот что можем предложить сейчас:\n\n{descriptions}\n\nЧто сейчас ближе?"


def _with_safety_side_effect(risk: RiskAssessment, decision: ResolvedTurn) -> ResolvedTurn:
    if risk.level not in {RiskLevel.CONCERN, RiskLevel.URGENT}:
        return decision
    return decision.model_copy(
        update={"side_effects": (*decision.side_effects, PolicySideEffect.RECORD_SAFETY)}
    )


def _is_consistent(plan: SupportPlan) -> bool:
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


def _starts_new_workflow(plan: SupportPlan) -> bool:
    return plan.next_action in {
        SupportAction.OFFER_AID,
        SupportAction.START_PSYCHOLOGIST_REQUEST,
    }

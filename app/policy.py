"""Backend-owned workflow projection over structured Qwen diagnostics.

Qwen classifies risk, conversational intent, and relevant kinds of help.  This
module owns only durable state transitions, catalogue callbacks, and the safe
copy for an already-classified crisis.  It intentionally contains no lexical
or regular-expression interpretation of a user's text.
"""

from __future__ import annotations

from app.catalog import available_aid_for_need, get_aid_item
from app.domain import (
    ChoiceSet,
    ConversationState,
    DiagnosticStatus,
    NeedKind,
    PolicyContext,
    PolicyEffect,
    PolicySideEffect,
    ResolvedTurn,
    RiskAssessment,
    RiskLevel,
    SafetyCategory,
    SafetyEscalation,
    SupportIntent,
    SupportOffer,
)

POLICY_VERSION = "model-routing-v1"
_SAFE_FALLBACK = "Я рядом и готова продолжить. Можно написать, что сейчас важно."
_MODEL_UNAVAILABLE_PROMPT = "Я здесь. Можно продолжить разговор или позвать человека."
HUMAN_HANDOFF_PROMPT = (
    "Слышу вас. Зову человека, который работает с этим ресурсом. Здесь можно продолжать писать."
)
CONTACT_PROMPT = "Чтобы это передать, нужен удобный способ связи. Что вам подходит?"
MORE_HELP_PROMPT = "Хорошо, запрос сохранён. Нужно что-то ещё?"
NEED_DISCOVERY_PROMPT = "Что сейчас важнее всего? Можно выбрать или написать своими словами."
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


def model_risk_assessment(context: PolicyContext) -> RiskAssessment:
    """Project only a completed structured safety response into audit data."""
    if context.safety_status is DiagnosticStatus.COMPLETED and context.safety is not None:
        return RiskAssessment(
            level=context.safety.level,
            escalation=context.safety.escalation,
            categories=context.safety.categories,
            confidence=context.safety.confidence,
            rationale=context.safety.rationale,
            detector="qwen-risk",
        )
    return RiskAssessment(
        level=RiskLevel.UNKNOWN,
        escalation=SafetyEscalation.NONE,
        rationale="risk diagnostic unavailable",
        detector="qwen-risk",
    )


def resolve_turn(context: PolicyContext) -> ResolvedTurn:
    """Resolve a message from model diagnostics and backend-owned workflow state."""
    assessment = model_risk_assessment(context)
    if assessment.escalation is SafetyEscalation.SUICIDE:
        return _finalize_turn(context, safety_resolved_turn(context, assessment))
    if _is_plain_direct_human_request(assessment):
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=HUMAN_HANDOFF_PROMPT,
                choice_set=ChoiceSet.SAFE_CONTINUE,
                effect=PolicyEffect.HUMAN_HANDOFF,
            ),
        )

    support = context.support
    if (
        context.support_status is DiagnosticStatus.COMPLETED
        and support is not None
        and support.intent is SupportIntent.EXPLICIT_HUMAN_REQUEST
    ):
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=HUMAN_HANDOFF_PROMPT,
                choice_set=ChoiceSet.SAFE_CONTINUE,
                effect=PolicyEffect.HUMAN_HANDOFF,
            ),
        )
    if context.safety_status is not DiagnosticStatus.COMPLETED:
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=_MODEL_UNAVAILABLE_PROMPT,
                choice_set=ChoiceSet.SAFE_CONTINUE,
                fallback_reason="safety_diagnostic_unavailable",
            ),
        )
    if assessment.escalation is SafetyEscalation.HANDOFF or assessment.level is RiskLevel.CRITICAL:
        return _finalize_turn(context, safety_resolved_turn(context, assessment))
    if context.support_status is not DiagnosticStatus.COMPLETED or context.support is None:
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=_MODEL_UNAVAILABLE_PROMPT,
                choice_set=ChoiceSet.SAFE_CONTINUE,
                fallback_reason="support_diagnostic_unavailable",
            ),
        )

    assert support is not None
    if context.state in _FINITE_WORKFLOW_STATES:
        return resolve_workflow_turn(context)
    if support.intent is SupportIntent.PSYCHOLOGIST_REQUEST:
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=CONTACT_PROMPT,
                choice_set=ChoiceSet.CONTACT_METHODS,
                effect=PolicyEffect.START_PSYCHOLOGIST_REQUEST,
            ),
        )
    if support.intent is SupportIntent.AID_INTEREST:
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=NEED_DISCOVERY_PROMPT,
                choice_set=ChoiceSet.NEED_CATEGORIES,
                effect=PolicyEffect.START_NEED_DISCOVERY,
            ),
        )
    if (
        context.pending_offer is SupportOffer.PSYCHOLOGIST
        and support.intent is SupportIntent.PSYCHOLOGIST_CONSIDERING
    ):
        return _finalize_turn(
            context,
            ResolvedTurn(
                text="Могу рассказать о поддержке психолога. Если захотите, можно оставить удобный контакт.",
                choice_set=ChoiceSet.PSYCHOLOGIST_INTEREST,
            ),
        )
    return _finalize_turn(context, _open_conversation_turn(context))


def resolve_workflow_turn(context: PolicyContext) -> ResolvedTurn:
    """Continue a workflow created by a callback without reclassifying its payload."""
    if context.state == ConversationState.COLLECTING_LOCATION.value:
        decision = ResolvedTurn(
            text=CONTACT_PROMPT,
            choice_set=ChoiceSet.CONTACT_METHODS,
            effect=PolicyEffect.CAPTURE_LOCATION,
            workflow_value=context.workflow_value[:120],
        )
    elif context.state == ConversationState.COLLECTING_CONTACT_VALUE.value:
        decision = ResolvedTurn(
            text=MORE_HELP_PROMPT,
            choice_set=ChoiceSet.MORE_HELP,
            effect=PolicyEffect.COMPLETE_CONTACT,
            workflow_value=context.workflow_value.strip()[:320],
        )
    elif context.state == ConversationState.CHOOSING_AID.value:
        try:
            item_ids = _catalog_item_ids(NeedKind(context.need or ""))
        except ValueError:
            item_ids = ()
        decision = ResolvedTurn(
            text=_aid_offer_text(item_ids) if item_ids else _SAFE_FALLBACK,
            choice_set=ChoiceSet.AID_CATALOG if item_ids else ChoiceSet.NONE,
            effect=PolicyEffect.REPLAY_WORKFLOW,
            catalog_item_ids=item_ids,
            fallback_reason=None if item_ids else "workflow_invalid",
        )
    elif context.state == ConversationState.COLLECTING_CONTACT_METHOD.value:
        decision = ResolvedTurn(
            text=CONTACT_PROMPT,
            choice_set=ChoiceSet.CONTACT_METHODS,
            effect=PolicyEffect.REPLAY_WORKFLOW,
        )
    elif context.state == ConversationState.AID_REQUESTED.value:
        decision = ResolvedTurn(
            text=MORE_HELP_PROMPT,
            choice_set=ChoiceSet.MORE_HELP,
            effect=PolicyEffect.REPLAY_WORKFLOW,
        )
    elif context.state == ConversationState.FOLLOWUP_SENT.value:
        decision = ResolvedTurn(text=_SAFE_FALLBACK)
    else:
        decision = ResolvedTurn(
            text=_SAFE_FALLBACK,
            effect=PolicyEffect.REPLAY_WORKFLOW,
            fallback_reason="workflow_active",
        )
    return _finalize_turn(context, decision)


def safety_resolved_turn(context: PolicyContext, risk: RiskAssessment) -> ResolvedTurn:
    if risk.escalation is SafetyEscalation.SUICIDE or SafetyCategory.SUICIDE in risk.categories:
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
        effect=PolicyEffect.SAFETY_ESCALATION,
        need=_primary_escalation_need(context, risk),
        side_effects=(PolicySideEffect.RECORD_SAFETY,),
    )


def _primary_escalation_need(context: PolicyContext, risk: RiskAssessment) -> NeedKind | None:
    """Keep one relevant post-S11 route without interpreting raw user text locally."""
    if SafetyCategory.CHILD_SAFETY in risk.categories:
        return NeedKind.CHILDREN
    if SafetyCategory.ACUTE_HOMELESSNESS in risk.categories:
        return NeedKind.HOUSING
    if context.support is None:
        return None
    for need in (
        NeedKind.CHILDREN,
        NeedKind.HOUSING,
        NeedKind.LEGAL,
        NeedKind.FOOD_MONEY,
        NeedKind.SUPPORT,
        NeedKind.OTHER,
    ):
        if need in context.support.need_hints:
            return need
    return None


def _is_plain_direct_human_request(risk: RiskAssessment) -> bool:
    return risk.categories == (SafetyCategory.DIRECT_HUMAN_REQUEST,)


def _open_conversation_turn(context: PolicyContext) -> ResolvedTurn:
    assert context.support is not None
    return ResolvedTurn(
        text=context.support.draft_text.strip() or _SAFE_FALLBACK,
        choice_set=ChoiceSet.CONTEXTUAL_NEEDS if context.support.need_hints else ChoiceSet.NONE,
        contextual_needs=tuple(dict.fromkeys(context.support.need_hints)),
        offered_support=context.support.suggested_support,
    )


def _catalog_item_ids(need: NeedKind | None) -> tuple[str, ...]:
    if need is None:
        return ()
    return tuple(
        item.id for item in available_aid_for_need(need) if get_aid_item(item.id) is not None
    )


def _aid_offer_text(item_ids: tuple[str, ...]) -> str:
    descriptions = "\n".join(
        f"— {item.label}" for item_id in item_ids if (item := get_aid_item(item_id)) is not None
    )
    return f"Вот что можем предложить сейчас:\n\n{descriptions}\n\nЧто сейчас ближе?"


def _finalize_turn(context: PolicyContext, decision: ResolvedTurn) -> ResolvedTurn:
    assessment = model_risk_assessment(context)
    side_effects = decision.side_effects
    if (
        assessment.level in {RiskLevel.CONCERN, RiskLevel.URGENT}
        and decision.effect is not PolicyEffect.HUMAN_HANDOFF
        and PolicySideEffect.RECORD_SAFETY not in side_effects
    ):
        side_effects = (*side_effects, PolicySideEffect.RECORD_SAFETY)
    if (
        context.state == ConversationState.FOLLOWUP_SENT.value
        and PolicySideEffect.COMPLETE_FOLLOWUP not in side_effects
    ):
        side_effects = (*side_effects, PolicySideEffect.COMPLETE_FOLLOWUP)
    return decision.model_copy(update={"side_effects": side_effects})

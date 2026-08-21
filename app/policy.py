"""Deterministic owner of text-turn effects, workflows, and contextual choices."""

from __future__ import annotations

from unicodedata import normalize

from app.catalog import available_aid_for_need, get_aid_item
from app.domain import (
    ChoiceSet,
    ConversationState,
    DiagnosticStatus,
    HardSignalKind,
    NeedKind,
    PolicyContext,
    PolicyEffect,
    PolicySideEffect,
    ResolvedTurn,
    RiskAssessment,
    RiskLevel,
    SupportOffer,
)

POLICY_VERSION = "deterministic-policy-v2"
_SAFE_FALLBACK = "Я рядом и готова продолжить. Можно написать, что сейчас важно."
_LOCAL_UNAVAILABLE_PROMPT = "Я здесь. Можно продолжить разговор или позвать человека."
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
_EXTERNAL_COMPLETION_FAMILIES = (
    (
        frozenset({"заявка", "заявку", "заявки", "заявке", "запрос", "запроса", "запросу", "запросом"}),
        frozenset(
            {
                "сохранена",
                "сохранен",
                "сохранены",
                "сохранил",
                "сохранила",
                "сохранили",
                "принята",
                "принят",
                "приняты",
                "принял",
                "приняла",
                "приняли",
                "зарегистрирована",
                "зарегистрирован",
                "зарегистрированы",
                "зарегистрировал",
                "зарегистрировала",
                "зарегистрировали",
                "оформлена",
                "оформлен",
                "оформлены",
                "оформил",
                "оформила",
                "оформили",
                "отправлена",
                "отправлен",
                "отправлены",
                "отправил",
                "отправила",
                "отправили",
            }
        ),
    ),
    (
        frozenset({"данные", "данных", "данными", "информация", "информацию", "информации", "контакт", "контакта", "контакты", "контактов"}),
        frozenset(
            {
                "отправлен",
                "отправлена",
                "отправлены",
                "отправил",
                "отправила",
                "отправили",
                "передан",
                "передана",
                "переданы",
                "передал",
                "передала",
                "передали",
                "получен",
                "получена",
                "получены",
                "получил",
                "получила",
                "получили",
            }
        ),
    ),
    (
        frozenset({"помощь", "помощи"}),
        frozenset({"организована", "организован", "организованы", "организовал", "организовала", "организовали"}),
    ),
    (
        frozenset({"оператор", "оператора", "оператором", "человек", "человека", "человеком", "специалист", "специалиста", "специалистом", "специалисткой"}),
        frozenset({"подключен", "подключена", "подключены", "подключил", "подключила", "подключили", "позвал", "позвала", "позвали", "связались"}),
    ),
)
_EXTERNAL_COMPLETION_PHRASES = (
    ("с", "вами", "свяжутся"),
    ("с", "тобой", "свяжутся"),
    ("вас", "уже", "записали"),
    ("вы", "уже", "записаны"),
)
_NON_COMPLETION_TOKENS = frozenset({"не", "может", "могут", "могу", "можем", "будет", "будут", "если"})


def resolve_turn(context: PolicyContext) -> ResolvedTurn:
    """Resolve the one permitted deterministic projection for a text turn.

    Agent labels are deliberately absent from authorization conditions. A completed support
    diagnostic can contribute only guarded conversational wording and a soft pending offer.
    """
    if context.local_risk.level is RiskLevel.CRITICAL:
        return _finalize_turn(context, critical_resolved_turn(context.local_risk))
    if _has_signal(context, HardSignalKind.EXPLICIT_HUMAN_REQUEST):
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=HUMAN_HANDOFF_PROMPT,
                choice_set=ChoiceSet.SAFE_CONTINUE,
                effect=PolicyEffect.HUMAN_HANDOFF,
            ),
        )
    if context.signals is None or context.local_risk.level is RiskLevel.UNKNOWN:
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=_LOCAL_UNAVAILABLE_PROMPT,
                choice_set=ChoiceSet.SAFE_CONTINUE,
                fallback_reason="local_input_unavailable",
            ),
        )
    if context.state in _FINITE_WORKFLOW_STATES:
        return resolve_workflow_turn(context)
    if _has_signal(context, HardSignalKind.PSYCHOLOGIST_REQUEST):
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=CONTACT_PROMPT,
                choice_set=ChoiceSet.CONTACT_METHODS,
                effect=PolicyEffect.START_PSYCHOLOGIST_REQUEST,
            ),
        )
    if _has_signal(context, HardSignalKind.CONCRETE_AID):
        need = _concrete_need(context)
        item_ids = _catalog_item_ids(need)
        if need is not None and item_ids:
            return _finalize_turn(
                context,
                ResolvedTurn(
                    text=_aid_offer_text(item_ids),
                    choice_set=ChoiceSet.AID_CATALOG,
                    effect=PolicyEffect.OFFER_AID,
                    need=need,
                    catalog_item_ids=item_ids,
                ),
            )
    if _has_signal(context, HardSignalKind.GENERIC_AID_INTEREST):
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
        and _has_signal(context, HardSignalKind.PSYCHOLOGIST_CONSIDERING)
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
    """Replay the active finite workflow without reinterpreting the input as a new flow."""
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
        # A text reply completes the reminder before returning to the ordinary conversation.
        # The finalizer attaches COMPLETE_FOLLOWUP; no model label can alter this route.
        decision = ResolvedTurn(text=_SAFE_FALLBACK)
    else:
        decision = ResolvedTurn(
            text=_SAFE_FALLBACK,
            effect=PolicyEffect.REPLAY_WORKFLOW,
            fallback_reason="workflow_active",
        )
    return _finalize_turn(context, decision)


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


def _open_conversation_turn(context: PolicyContext) -> ResolvedTurn:
    if context.support_status is not DiagnosticStatus.COMPLETED or context.support is None:
        return ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="support_diagnostic_unavailable")
    draft = context.support.draft_text.strip()
    if not draft or _claims_external_action(draft):
        return ResolvedTurn(text=_SAFE_FALLBACK, fallback_reason="support_draft_guard")
    return ResolvedTurn(
        text=draft,
        offered_support=context.support.suggested_support,
    )


def _claims_external_action(draft: str) -> bool:
    tokens = _draft_tokens(draft)
    return any(_contains_token_phrase(tokens, phrase) for phrase in _EXTERNAL_COMPLETION_PHRASES) or any(
        _contains_completed_family(tokens, referents, completions)
        for referents, completions in _EXTERNAL_COMPLETION_FAMILIES
    )


def _draft_tokens(draft: str) -> tuple[str, ...]:
    normalized = normalize("NFKC", draft).casefold().replace("ё", "е")
    tokens: list[str] = []
    current: list[str] = []
    for char in normalized:
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current.clear()
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _contains_token_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    for index in range(len(tokens) - width + 1):
        if tokens[index : index + width] != phrase:
            continue
        if not _has_noncompletion_context(tokens, index, index + width):
            return True
    return False


def _contains_completed_family(
    tokens: tuple[str, ...],
    referents: frozenset[str],
    completions: frozenset[str],
) -> bool:
    for referent_index, token in enumerate(tokens):
        if token not in referents:
            continue
        start = max(0, referent_index - 3)
        end = min(len(tokens), referent_index + 4)
        for completion_index in range(start, end):
            if tokens[completion_index] not in completions:
                continue
            phrase_start = min(referent_index, completion_index)
            phrase_end = max(referent_index, completion_index) + 1
            if not _has_noncompletion_context(tokens, phrase_start, phrase_end):
                return True
    return False


def _has_noncompletion_context(tokens: tuple[str, ...], start: int, end: int) -> bool:
    return bool(_NON_COMPLETION_TOKENS.intersection(tokens[max(0, start - 2) : end]))


def _has_signal(context: PolicyContext, kind: HardSignalKind) -> bool:
    return context.signals is not None and any(match.kind is kind for match in context.signals.matches)


def _concrete_need(context: PolicyContext) -> NeedKind | None:
    if context.signals is None:
        return None
    return next((match.need for match in context.signals.matches if match.kind is HardSignalKind.CONCRETE_AID), None)


def _catalog_item_ids(need: NeedKind | None) -> tuple[str, ...]:
    if need is None:
        return ()
    return tuple(item.id for item in available_aid_for_need(need) if get_aid_item(item.id) is not None)


def _aid_offer_text(item_ids: tuple[str, ...]) -> str:
    descriptions = "\n".join(
        f"— {item.label}" for item_id in item_ids if (item := get_aid_item(item_id)) is not None
    )
    return f"Вот что можем предложить сейчас:\n\n{descriptions}\n\nЧто сейчас ближе?"


def _finalize_turn(context: PolicyContext, decision: ResolvedTurn) -> ResolvedTurn:
    side_effects = decision.side_effects
    if (
        context.local_risk.level in {RiskLevel.CONCERN, RiskLevel.URGENT}
        and PolicySideEffect.RECORD_SAFETY not in side_effects
    ):
        side_effects = (*side_effects, PolicySideEffect.RECORD_SAFETY)
    if (
        context.state == ConversationState.FOLLOWUP_SENT.value
        and PolicySideEffect.COMPLETE_FOLLOWUP not in side_effects
    ):
        side_effects = (*side_effects, PolicySideEffect.COMPLETE_FOLLOWUP)
    return decision.model_copy(update={"side_effects": side_effects})

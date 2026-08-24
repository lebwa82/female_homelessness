"""Deterministic owner of text-turn effects, workflows, and contextual choices."""

from __future__ import annotations

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
from app.signals import ClauseBoundaryKind, scan_signal_input

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
_WORKFLOW_ESCAPE_STATES = _FINITE_WORKFLOW_STATES | {ConversationState.DISCOVERING_NEED.value}
_EXTERNAL_COMPLETION_FAMILIES = (
    (
        frozenset(
            {"заявка", "заявку", "заявки", "заявке", "запрос", "запроса", "запросу", "запросом"}
        ),
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
        frozenset(
            {
                "данные",
                "данных",
                "данными",
                "информация",
                "информацию",
                "информации",
                "контакт",
                "контакта",
                "контакты",
                "контактов",
            }
        ),
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
        frozenset(
            {
                "организована",
                "организован",
                "организованы",
                "организовал",
                "организовала",
                "организовали",
            }
        ),
    ),
    (
        frozenset(
            {
                "оператор",
                "оператора",
                "оператором",
                "человек",
                "человека",
                "человеком",
                "специалист",
                "специалиста",
                "специалистом",
                "специалисткой",
            }
        ),
        frozenset(
            {
                "подключен",
                "подключена",
                "подключены",
                "подключил",
                "подключила",
                "подключили",
                "позвал",
                "позвала",
                "позвали",
                "связались",
            }
        ),
    ),
)
_MAX_COMPLETION_TOKEN_GAP = 6
_MODAL_TOKENS = frozenset(
    {
        "мог",
        "могла",
        "могли",
        "могу",
        "могут",
        "можем",
        "может",
        "можете",
        "можешь",
        "смог",
        "смогла",
        "смогли",
        "смогу",
        "смогут",
        "сможем",
        "сможет",
        "сможете",
        "сможешь",
        "можно",
    }
)
_FUTURE_AUXILIARIES = frozenset({"буду", "будем", "будет", "будете", "будешь", "будут"})
_CONDITIONAL_LEADS = frozenset({"если"})
_CONDITIONAL_PREFIXES = frozenset({"а", "и", "но"})
_OPERATIONAL_ACTORS = frozenset(
    {
        "я",
        "мы",
        "оператор",
        "оператора",
        "специалист",
        "специалиста",
        "специалистка",
        "специалистке",
        "человек",
        "человека",
        "вам",
        "вас",
        "вы",
        "тобой",
        "тебе",
        "с",
        "вами",
    }
)
_OPERATIONAL_ACTION_STEMS = (
    "вызв",
    "зарегистр",
    "записа",
    "записыва",
    "запиш",
    "оформ",
    "отправ",
    "переда",
    "перезвон",
    "подключ",
    "позвон",
    "связа",
    "связыва",
    "свяж",
)
_FINITE_ACTION_SUFFIXES = (
    "у",
    "ю",
    "ем",
    "им",
    "ешь",
    "ишь",
    "ет",
    "ит",
    "ете",
    "ите",
    "ут",
    "ют",
    "ат",
    "ят",
    "л",
    "ла",
    "ли",
    "лся",
    "лась",
    "лись",
    "емся",
    "имся",
    "тся",
)


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
    if context.state in _WORKFLOW_ESCAPE_STATES and _has_signal(
        context, HardSignalKind.OPEN_CONVERSATION_REQUEST
    ):
        return _finalize_turn(
            context,
            ResolvedTurn(
                text=_SAFE_FALLBACK,
                effect=PolicyEffect.CANCEL_WORKFLOW,
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
    if context.pending_offer is SupportOffer.PSYCHOLOGIST and _has_signal(
        context, HardSignalKind.PSYCHOLOGIST_CONSIDERING
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
    signal_input = scan_signal_input(draft)
    previous_was_leading_condition = False
    for clause in signal_input.clauses:
        tokens = signal_input.values[clause.token_start : clause.token_end]
        leading_condition = _has_leading_condition(tokens)
        inherited_condition = previous_was_leading_condition
        if _contains_operational_claim(
            tokens,
            conditional=leading_condition or inherited_condition,
        ) or any(
            _contains_completed_family(
                tokens,
                referents,
                completions,
                leading_condition=leading_condition,
                inherited_condition=inherited_condition,
            )
            for referents, completions in _EXTERNAL_COMPLETION_FAMILIES
        ):
            return True
        previous_was_leading_condition = bool(
            leading_condition
            and clause.boundary_after is not None
            and clause.boundary_after.kind in {ClauseBoundaryKind.COMMA, ClauseBoundaryKind.DASH}
        )
    return False


def _has_leading_condition(tokens: tuple[str, ...]) -> bool:
    if not tokens:
        return False
    if tokens[0] in _CONDITIONAL_LEADS:
        return True
    return (
        len(tokens) > 1 and tokens[0] in _CONDITIONAL_PREFIXES and tokens[1] in _CONDITIONAL_LEADS
    )


def _contains_operational_claim(
    tokens: tuple[str, ...],
    *,
    conditional: bool,
) -> bool:
    """Detect bounded actor/action/referent claims across reviewed inflections.

    Predicate-local negation and modality suppress only the predicate they
    govern.  A leading conditional clause scopes the immediately following
    comma/dash-delimited clause, rather than making every later token optional.
    """
    for action_index, action in enumerate(tokens):
        if not _is_operational_predicate(action):
            continue
        start = max(0, action_index - _MAX_COMPLETION_TOKEN_GAP)
        end = min(len(tokens), action_index + _MAX_COMPLETION_TOKEN_GAP + 1)
        nearby = tokens[start:end]
        has_actor = bool(_OPERATIONAL_ACTORS.intersection(nearby))
        has_referent = any(
            referent in nearby
            for referents, _ in _EXTERNAL_COMPLETION_FAMILIES
            for referent in referents
        )
        if not (has_actor or has_referent):
            continue
        if _predicate_is_negated(tokens, action_index) or _predicate_is_modal(
            tokens,
            action_index,
        ):
            continue
        if conditional:
            continue
        if _is_action_infinitive(action) and not _has_future_auxiliary(tokens, action_index):
            continue
        if not _is_action_infinitive(action) or _has_future_auxiliary(tokens, action_index):
            return True
    return False


def _is_operational_predicate(token: str) -> bool:
    if not token.startswith(_OPERATIONAL_ACTION_STEMS):
        return False
    return _is_action_infinitive(token) or token.endswith(_FINITE_ACTION_SUFFIXES)


def _is_action_infinitive(token: str) -> bool:
    return token.endswith(("ть", "ться", "ти"))


def _predicate_is_negated(tokens: tuple[str, ...], predicate_index: int) -> bool:
    return predicate_index > 0 and tokens[predicate_index - 1] == "не"


def _predicate_is_modal(tokens: tuple[str, ...], predicate_index: int) -> bool:
    start = max(0, predicate_index - 3)
    return any(token in _MODAL_TOKENS for token in tokens[start:predicate_index])


def _has_future_auxiliary(tokens: tuple[str, ...], predicate_index: int) -> bool:
    start = max(0, predicate_index - 3)
    return any(token in _FUTURE_AUXILIARIES for token in tokens[start:predicate_index])


def _contains_completed_family(
    tokens: tuple[str, ...],
    referents: frozenset[str],
    completions: frozenset[str],
    *,
    leading_condition: bool,
    inherited_condition: bool,
) -> bool:
    for referent_index, token in enumerate(tokens):
        if token not in referents:
            continue
        start = max(0, referent_index - _MAX_COMPLETION_TOKEN_GAP)
        end = min(len(tokens), referent_index + _MAX_COMPLETION_TOKEN_GAP + 1)
        for completion_index in range(start, end):
            if tokens[completion_index] not in completions:
                continue
            if _predicate_is_negated(tokens, completion_index) or _predicate_is_modal(
                tokens,
                completion_index,
            ):
                continue
            if leading_condition:
                continue
            if inherited_condition and _has_future_auxiliary(tokens, completion_index):
                continue
            return True
    return False


def _has_signal(context: PolicyContext, kind: HardSignalKind) -> bool:
    return context.signals is not None and any(
        match.kind is kind for match in context.signals.matches
    )


def _concrete_need(context: PolicyContext) -> NeedKind | None:
    if context.signals is None:
        return None
    return next(
        (
            match.need
            for match in context.signals.matches
            if match.kind is HardSignalKind.CONCRETE_AID
        ),
        None,
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

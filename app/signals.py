from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from unicodedata import normalize

from app.domain import DeterministicSignals, HardSignalKind, NeedKind, SignalMatch, SupportOffer

MATCHER_VERSION = "deterministic-signals-v2"

_HUMAN_TRANSFER_VERBS = frozenset({"позови", "позовите", "переключи", "переключите", "соедини", "соедините"})
_HUMAN_TRANSFER_FILLERS = frozenset({"меня", "на", "с", "к", "живого", "живым", "пожалуйста"})
_HUMAN_ROLES = frozenset(
    {
        "человек",
        "человека",
        "человеком",
        "оператор",
        "оператора",
        "оператором",
        "специалист",
        "специалиста",
        "специалистом",
        "специалисткой",
        "специалистку",
    }
)
_BOT_WORDS = frozenset({"бот", "бота", "ботом"})
_IMMEDIATE_MARKERS = frozenset({"сейчас", "сегодня", "ночью", "срочно"})
_ASSAULT_WORDS = frozenset({"бьет", "бьют", "избивает", "избивают"})
_THREAT_WORDS = frozenset({"угрожает", "угрожают"})
_EVICTION_WORDS = frozenset({"выгнали", "выселили"})


def extract_signals(
    text: str,
    *,
    pending_offer: SupportOffer | None = None,
) -> DeterministicSignals:
    """Extract bounded token-sequence signals without retaining the input text."""
    tokens = _scan_tokens(text)
    matches: list[SignalMatch] = []
    seen: set[tuple[HardSignalKind, str, int, int, NeedKind | None]] = set()

    def add(
        kind: HardSignalKind,
        rule_id: str,
        token_start: int,
        token_end: int,
        need: NeedKind | None = None,
    ) -> None:
        key = (kind, rule_id, token_start, token_end, need)
        if key not in seen:
            seen.add(key)
            matches.append(
                SignalMatch(
                    kind=kind,
                    rule_id=rule_id,
                    token_start=token_start,
                    token_end=token_end,
                    need=need,
                )
            )

    _add_human_request_matches(tokens, add)
    _add_open_conversation_matches(tokens, add)
    _add_aid_matches(tokens, add)
    _add_psychologist_matches(tokens, add)
    _add_pending_offer_matches(tokens, pending_offer, add)
    _add_safety_matches(tokens, add)

    return DeterministicSignals(
        matcher_version=MATCHER_VERSION,
        input_hash=sha256(text.encode("utf-8")).hexdigest(),
        matches=tuple(sorted(matches, key=lambda match: (match.token_start, match.token_end, match.rule_id))),
    )


def _scan_tokens(text: str) -> tuple[str, ...]:
    normalized = normalize("NFKC", text).casefold().replace("ё", "е")
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


def _add_human_request_matches(tokens: tuple[str, ...], add: _AddMatch) -> None:
    for phrase in (
        ("не", "хочу", "говорить", "с"),
        ("не", "хочу", "общаться", "с"),
        ("а", "не", "с"),
    ):
        for start in _find_phrase(tokens, phrase):
            end = start + len(phrase)
            if end < len(tokens) and tokens[end] in _BOT_WORDS:
                add(HardSignalKind.EXPLICIT_HUMAN_REQUEST, "human.reject_bot", start, end + 1)

    for start, token in enumerate(tokens):
        if token not in _HUMAN_TRANSFER_VERBS:
            continue
        for role_index in range(start + 1, min(start + 5, len(tokens))):
            if tokens[role_index] not in _HUMAN_ROLES:
                continue
            if all(part in _HUMAN_TRANSFER_FILLERS for part in tokens[start + 1 : role_index]):
                add(HardSignalKind.EXPLICIT_HUMAN_REQUEST, "human.transfer.role", start, role_index + 1)
            break

    for lead in ("хочу", "можно"):
        for preposition in ("с", "со"):
            for start in _find_phrase(tokens, (lead, "поговорить", preposition)):
                if _is_predicate_negated(tokens, start):
                    continue
                for role_index in range(start + 3, min(start + 6, len(tokens))):
                    if tokens[role_index] in _HUMAN_ROLES:
                        if all(part in _HUMAN_TRANSFER_FILLERS for part in tokens[start + 3 : role_index]):
                            add(HardSignalKind.EXPLICIT_HUMAN_REQUEST, "human.want_talk.role", start, role_index + 1)
                        break


def _add_open_conversation_matches(tokens: tuple[str, ...], add: _AddMatch) -> None:
    """Recognise a bounded request to leave a completed workflow for ordinary conversation."""
    for phrase in (
        ("можно", "просто", "поговорить"),
        ("хочу", "просто", "поговорить"),
        ("хочу", "выговориться"),
        ("хочу", "продолжить", "разговор"),
        ("поговори", "со", "мной"),
        ("поговорите", "со", "мной"),
        ("выслушай", "меня"),
        ("выслушайте", "меня"),
        ("отмена",),
        ("отменить",),
        ("не", "хочу", "продолжать"),
        ("не", "нужно", "продолжать"),
        ("не", "нужна", "заявка"),
        ("не", "хочу", "указывать", "город"),
        ("не", "хочу", "указывать", "место"),
        ("не", "хочу", "оставлять", "контакт"),
        ("не", "буду", "оставлять", "контакт"),
        ("пропустить",),
    ):
        for start in _find_phrase(tokens, phrase):
            add(
                HardSignalKind.OPEN_CONVERSATION_REQUEST,
                "conversation.continue.explicit",
                start,
                start + len(phrase),
            )


def _add_aid_matches(tokens: tuple[str, ...], add: _AddMatch) -> None:
    for start in _find_phrase(tokens, ("негде", "ночевать")):
        add(HardSignalKind.CONCRETE_AID, "aid.housing.no_shelter", start, start + 2, NeedKind.HOUSING)
    for start in _find_phrase(tokens, ("нет", "где", "ночевать")):
        add(HardSignalKind.CONCRETE_AID, "aid.housing.no_shelter", start, start + 3, NeedKind.HOUSING)
    for start, token in enumerate(tokens):
        if token in _EVICTION_WORDS and not _is_predicate_negated(tokens, start):
            add(HardSignalKind.CONCRETE_AID, "aid.housing.eviction", start, start + 1, NeedKind.HOUSING)
    for phrase, rule_id, need in (
        (("нужны", "продукты"), "aid.food.products", NeedKind.FOOD_MONEY),
        (("нужна", "карточка", "на", "еду"), "aid.food.card", NeedKind.FOOD_MONEY),
        (("потеряла", "паспорт"), "aid.legal.passport", NeedKind.LEGAL),
        (("нужен", "юрист"), "aid.legal.lawyer", NeedKind.LEGAL),
        (("нужна", "помощь", "с", "документами"), "aid.legal.documents", NeedKind.LEGAL),
        (("нужна", "помощь", "с", "проездом"), "aid.transport", NeedKind.FOOD_MONEY),
    ):
        for start in _find_phrase(tokens, phrase):
            if not _is_predicate_negated(tokens, start):
                add(HardSignalKind.CONCRETE_AID, rule_id, start, start + len(phrase), need)
    for phrase in (("нужны", "вещи", "для", "ребенка"), ("не", "хватает", "вещей", "для", "ребенка")):
        for start in _find_phrase(tokens, phrase):
            if not _is_predicate_negated(tokens, start):
                add(
                    HardSignalKind.CONCRETE_AID,
                    "aid.children.items",
                    start,
                    start + len(phrase),
                    NeedKind.CHILDREN,
                )
    for phrase in (("какую", "практическую", "помощь", "можно", "получить"), ("какую", "помощь", "можно", "получить")):
        for start in _find_phrase(tokens, phrase):
            add(HardSignalKind.GENERIC_AID_INTEREST, "aid.generic.available", start, start + len(phrase))


def _add_psychologist_matches(tokens: tuple[str, ...], add: _AddMatch) -> None:
    for start in _find_phrase(tokens, ("хочу", "поговорить", "с", "психологом")):
        if not _is_predicate_negated(tokens, start):
            add(HardSignalKind.PSYCHOLOGIST_REQUEST, "psychologist.accept", start, start + 4)
    for start in _find_phrase(tokens, ("запишите", "меня", "к", "психологу")):
        if not _is_predicate_negated(tokens, start):
            add(HardSignalKind.PSYCHOLOGIST_REQUEST, "psychologist.book", start, start + 4)
    for start in _find_phrase(tokens, ("расскажите", "про", "психолога")):
        add(HardSignalKind.PSYCHOLOGIST_CONSIDERING, "psychologist.explain", start, start + 3)
    for start in _find_phrase(tokens, ("возможно", "психолог", "мог", "бы", "помочь")):
        add(HardSignalKind.PSYCHOLOGIST_CONSIDERING, "psychologist.tentative", start, start + 5)
    for start in _find_phrase(tokens, ("не", "уверена", "насчет", "психолога")):
        add(HardSignalKind.PSYCHOLOGIST_CONSIDERING, "psychologist.uncertain", start, start + 4)


def _add_pending_offer_matches(
    tokens: tuple[str, ...],
    pending_offer: SupportOffer | None,
    add: _AddMatch,
) -> None:
    """Interpret only bounded acknowledgement language after a backend-owned offer."""
    if pending_offer is not SupportOffer.PSYCHOLOGIST:
        return
    if tokens == ("да", "хочу"):
        add(HardSignalKind.PSYCHOLOGIST_REQUEST, "psychologist.pending.accept", 0, 2)
    for start, token in enumerate(tokens):
        if token != "расскажите":
            continue
        end = start + 1
        if end < len(tokens) and tokens[end] == "пожалуйста":
            end += 1
        add(HardSignalKind.PSYCHOLOGIST_CONSIDERING, "psychologist.pending.explain", start, end)
    for start in _find_phrase(tokens, ("как", "проходят", "встречи")):
        add(HardSignalKind.PSYCHOLOGIST_CONSIDERING, "psychologist.pending.format", start, start + 3)


def _add_safety_matches(tokens: tuple[str, ...], add: _AddMatch) -> None:
    for phrase in (("не", "хочу", "жить"), ("не", "хочу", "больше", "жить")):
        for start in _find_phrase(tokens, phrase):
            if _is_bounded_not_want_to_live(tokens, start, len(phrase)):
                add(
                    HardSignalKind.SUICIDE_OR_SELF_HARM,
                    "safety.suicide.not_want_to_live",
                    start,
                    start + len(phrase),
                )
    for start in _find_phrase(tokens, ("хочу", "покончить", "с", "собой")):
        if not _is_predicate_negated(tokens, start):
            add(HardSignalKind.SUICIDE_OR_SELF_HARM, "safety.suicide.end_life", start, start + 4)
    for phrase, rule_id in (
        (("хочу", "умереть"), "safety.suicide.want_to_die"),
        (("убью", "себя"), "safety.suicide.kill_self"),
        (("хочу", "сейчас", "причинить", "себе", "вред"), "safety.self_harm.now"),
        (("причиню", "себе", "вред"), "safety.self_harm.now"),
    ):
        for start in _find_phrase(tokens, phrase):
            if not _is_predicate_negated(tokens, start):
                add(HardSignalKind.SUICIDE_OR_SELF_HARM, rule_id, start, start + len(phrase))
    for start, token in enumerate(tokens):
        if token in _ASSAULT_WORDS:
            marker = _nearby_marker(tokens, start, start + 1)
            if marker is not None:
                token_start, token_end = _marker_action_span(marker, start, start + 1)
                if not _is_predicate_negated(tokens, start):
                    add(
                        HardSignalKind.VIOLENCE_OR_THREAT_NOW,
                        "safety.violence.assault_now",
                        token_start,
                        token_end,
                    )
        if token in _THREAT_WORDS:
            marker = _nearby_marker(tokens, start, start + 1)
            if marker is None:
                if not _is_predicate_negated(tokens, start):
                    add(HardSignalKind.SAFETY_CONCERN, "safety.concern.ongoing_threat", start, start + 1)
            else:
                token_start, token_end = _marker_action_span(marker, start, start + 1)
                if not _is_predicate_negated(tokens, start):
                    add(
                        HardSignalKind.VIOLENCE_OR_THREAT_NOW,
                        "safety.violence.threat_now",
                        token_start,
                        token_end,
                    )

    for start, end in _shelter_phrase_spans(tokens):
        marker = _nearby_marker(tokens, start, end)
        if marker is None:
            add(HardSignalKind.SAFETY_CONCERN, "safety.concern.no_shelter", start, end)
        else:
            add(HardSignalKind.URGENT_SHELTER, "safety.shelter.no_shelter_now", min(marker, start), max(marker + 1, end))
    for start, token in enumerate(tokens):
        if token not in _EVICTION_WORDS:
            continue
        marker = _nearby_marker(tokens, start, start + 1)
        if marker is None and not _is_predicate_negated(tokens, start):
            add(HardSignalKind.SAFETY_CONCERN, "safety.concern.eviction", start, start + 1)
        elif marker is not None and not _is_predicate_negated(tokens, start):
            add(
                HardSignalKind.URGENT_SHELTER,
                "safety.shelter.eviction_now",
                min(marker, start),
                max(marker + 1, start + 1),
            )
    for start in _find_phrase(tokens, ("боюсь", "возвращаться")):
        token_end = start + 3 if start + 2 < len(tokens) and tokens[start + 2] == "домой" else start + 2
        add(HardSignalKind.SAFETY_CONCERN, "safety.concern.fear_returning", start, token_end)
    for start in _find_phrase(tokens, ("жилье", "нестабильное")):
        add(HardSignalKind.SAFETY_CONCERN, "safety.concern.unstable_housing", start, start + 2)


def _shelter_phrase_spans(tokens: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    spans = [(start, start + 2) for start in _find_phrase(tokens, ("негде", "ночевать"))]
    spans.extend((start, start + 3) for start in _find_phrase(tokens, ("нет", "где", "ночевать")))
    return tuple(spans)


def _find_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> tuple[int, ...]:
    return tuple(
        start
        for start in range(len(tokens) - len(phrase) + 1)
        if tokens[start : start + len(phrase)] == phrase
    )


def _nearby_marker(tokens: tuple[str, ...], start: int, end: int) -> int | None:
    window_start = max(0, start - 3)
    window_end = min(len(tokens), end + 3)
    for index in range(window_start, window_end):
        if tokens[index] in _IMMEDIATE_MARKERS:
            return index
    return None


def _marker_action_span(marker: int, start: int, end: int) -> tuple[int, int]:
    return min(marker, start), max(marker + 1, end)


def _is_bounded_not_want_to_live(tokens: tuple[str, ...], start: int, width: int) -> bool:
    """Keep direct suicidal language, but not residence or relationship grammar.

    A completed ``не хочу жить`` clause is itself a high-severity signal.  Only
    the two bounded continuations which change ``жить`` into a residence or a
    relationship predicate are excluded.  Ordinary distress after the clause
    must not weaken its local crisis route.
    """
    tail = tokens[start + width :]
    return not tail or tail[0] not in {"в", "с"}


def _is_predicate_negated(tokens: tuple[str, ...], predicate_start: int) -> bool:
    """Negation binds only to the following action predicate, never a later clause."""
    return predicate_start > 0 and tokens[predicate_start - 1] == "не"


type _AddMatch = Callable[[HardSignalKind, str, int, int, NeedKind | None], None]

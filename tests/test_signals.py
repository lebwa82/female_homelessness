from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain import HardSignalKind, NeedKind, SignalMatch, SupportOffer

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dialogue_scenarios.jsonl"


def _fixture_text(case_id: str) -> str:
    for line in FIXTURE_PATH.read_text().splitlines():
        row = json.loads(line)
        if row["id"] == case_id:
            return next(text for role, text in reversed(row["history"]) if role == "user")
    raise AssertionError(f"missing fixture case: {case_id}")


def _matches(
    text: str,
    *,
    pending_offer: SupportOffer | None = None,
) -> tuple[tuple[str, str, NeedKind | None], ...]:
    from app.signals import extract_signals

    return tuple(
        (match.kind.value, match.rule_id, match.need)
        for match in extract_signals(text, pending_offer=pending_offer).matches
    )


@pytest.mark.parametrize(
    ("case_id", "rule_id"),
    (
        ("human-01", "human.reject_bot"),
        ("human-02", "human.transfer.role"),
        ("human-03", "human.reject_bot"),
        ("human-04", "human.transfer.role"),
        ("human-05", "human.reject_bot"),
    ),
)
def test_all_explicit_human_fixture_rows_have_stable_hard_rules(
    case_id: str, rule_id: str
) -> None:
    matches = _matches(_fixture_text(case_id))

    assert ("explicit_human_request", rule_id, None) in matches


@pytest.mark.parametrize(
    "case_id",
    (
        "prod-listen-01",
        "listen-02",
        "near-miss-01",
        "near-miss-02",
        "near-miss-03",
        "near-miss-04",
        "open-01",
        "open-02",
        "open-03",
        "open-04",
        "open-05",
        "open-06",
        "open-07",
        "open-08",
        "open-09",
        "open-10",
        "open-11",
        "open-12",
    ),
)
def test_open_and_human_near_miss_rows_never_match_human_request(case_id: str) -> None:
    kinds = {kind for kind, _, _ in _matches(_fixture_text(case_id))}

    assert "explicit_human_request" not in kinds


@pytest.mark.parametrize(
    ("case_id", "kind", "rule_id", "need"),
    (
        ("aid-01", "concrete_aid", "aid.housing.no_shelter", NeedKind.HOUSING),
        ("aid-02", "concrete_aid", "aid.food.products", NeedKind.FOOD_MONEY),
        ("aid-03", "concrete_aid", "aid.food.card", NeedKind.FOOD_MONEY),
        ("aid-04", "concrete_aid", "aid.legal.passport", NeedKind.LEGAL),
        ("aid-05", "concrete_aid", "aid.legal.documents", NeedKind.LEGAL),
        ("aid-06", "concrete_aid", "aid.children.items", NeedKind.CHILDREN),
        ("aid-07", "concrete_aid", "aid.transport", NeedKind.FOOD_MONEY),
        ("aid-08", "generic_aid_interest", "aid.generic.available", None),
        (
            "multi-open-food-01",
            "concrete_aid",
            "aid.food.products",
            NeedKind.FOOD_MONEY,
        ),
    ),
)
def test_aid_fixture_rows_have_specific_or_generic_signals(
    case_id: str, kind: str, rule_id: str, need: NeedKind | None
) -> None:
    assert (kind, rule_id, need) in _matches(_fixture_text(case_id))


@pytest.mark.parametrize(
    ("case_id", "kind", "rule_id"),
    (
        ("psychologist-02", "psychologist_considering", "psychologist.explain"),
        ("psychologist-03", "psychologist_considering", "psychologist.tentative"),
        ("psychologist-05", "psychologist_considering", "psychologist.uncertain"),
        ("psychologist-request-01", "psychologist_request", "psychologist.accept"),
        ("psychologist-06", "psychologist_request", "psychologist.accept"),
        ("psychologist-07", "psychologist_request", "psychologist.book"),
        (
            "multi-psychologist-request-01",
            "psychologist_request",
            "psychologist.accept",
        ),
    ),
)
def test_explicit_psychologist_fixture_rows_have_stable_rules(
    case_id: str, kind: str, rule_id: str
) -> None:
    assert (kind, rule_id, None) in _matches(_fixture_text(case_id))


@pytest.mark.parametrize(
    "case_id",
    ("psychologist-considering-01", "psychologist-04"),
)
def test_psychologist_rows_that_require_pending_offer_context_have_no_text_only_signal(
    case_id: str,
) -> None:
    kinds = {kind for kind, _, _ in _matches(_fixture_text(case_id))}

    assert not {
        "psychologist_considering",
        "psychologist_request",
    }.intersection(kinds)


def test_pending_psychologist_acceptance_is_an_exact_next_turn_acknowledgement() -> None:
    assert (
        "psychologist_request",
        "psychologist.pending.accept",
        None,
    ) in _matches("да, хочу", pending_offer=SupportOffer.PSYCHOLOGIST)
    assert "psychologist_request" not in {
        kind
        for kind, _, _ in _matches("да, хочу продукты", pending_offer=SupportOffer.PSYCHOLOGIST)
    }


@pytest.mark.parametrize(
    ("case_id", "kind", "rule_id"),
    (
        ("suicide-01", "suicide_or_self_harm", "safety.suicide.end_life"),
        ("crisis-02", "suicide_or_self_harm", "safety.self_harm.now"),
        ("crisis-03", "violence_or_threat_now", "safety.violence.assault_now"),
        ("crisis-04", "violence_or_threat_now", "safety.violence.threat_now"),
        ("crisis-05", "urgent_shelter", "safety.shelter.no_shelter_now"),
        ("crisis-06", "urgent_shelter", "safety.shelter.eviction_now"),
        ("crisis-07", "safety_concern", "safety.concern.fear_returning"),
        ("crisis-08", "safety_concern", "safety.concern.unstable_housing"),
    ),
)
def test_crisis_fixture_rows_have_stable_safety_rules(
    case_id: str, kind: str, rule_id: str
) -> None:
    assert (kind, rule_id, None) in _matches(_fixture_text(case_id))


@pytest.mark.parametrize(
    ("text", "kind", "rule_id"),
    (
        ("не хочу жить", "suicide_or_self_harm", "safety.suicide.not_want_to_live"),
        ("боюсь возвращаться", "safety_concern", "safety.concern.fear_returning"),
    ),
)
def test_legacy_safety_phrases_remain_explicit_bounded_rules(
    text: str, kind: str, rule_id: str
) -> None:
    assert (kind, rule_id, None) in _matches(text)


@pytest.mark.parametrize(
    "text",
    ("не хочу жить", "не хочу больше жить"),
)
def test_terminal_existential_suicide_statements_have_bounded_hard_rules(text: str) -> None:
    assert ("suicide_or_self_harm", "safety.suicide.not_want_to_live", None) in _matches(text)


@pytest.mark.parametrize(
    "text",
    ("не хочу жить в этом городе", "не хочу жить с ним"),
)
def test_residence_or_relationship_continuations_do_not_match_suicide(text: str) -> None:
    kinds = {kind for kind, _, _ in _matches(text)}

    assert "suicide_or_self_harm" not in kinds


@pytest.mark.parametrize(
    "text",
    ("продукты лежат на столе", "паспорт уже готов", "документы в папке", "юрист позвонит позже"),
)
def test_bare_aid_narrative_nouns_do_not_create_concrete_aid_signal(text: str) -> None:
    assert _matches(text) == ()


@pytest.mark.parametrize("token_end", (3, 2))
def test_signal_match_requires_a_nonempty_half_open_token_span(token_end: int) -> None:
    with pytest.raises(ValidationError, match="token_end must be greater than token_start"):
        SignalMatch(
            kind=HardSignalKind.SUICIDE_OR_SELF_HARM,
            rule_id="test.invalid_span",
            token_start=3,
            token_end=token_end,
        )


@pytest.mark.parametrize(
    ("text", "rule_id"),
    (
        ("сейчас причиню себе вред", "safety.self_harm.now"),
        ("причиню себе вред сейчас", "safety.self_harm.now"),
        ("сейчас меня бьют", "safety.violence.assault_now"),
        ("меня бьют сейчас", "safety.violence.assault_now"),
        ("сейчас мне угрожают", "safety.violence.threat_now"),
        ("мне угрожают сейчас", "safety.violence.threat_now"),
    ),
)
def test_immediate_safety_markers_before_or_after_action_have_valid_spans(
    text: str, rule_id: str
) -> None:
    from app.signals import extract_signals

    match = next(match for match in extract_signals(text).matches if match.rule_id == rule_id)

    assert match.token_end > match.token_start


def test_ongoing_threat_is_concern_only_without_immediate_marker() -> None:
    assert _matches("он мне угрожает") == (
        ("safety_concern", "safety.concern.ongoing_threat", None),
    )
    assert ("violence_or_threat_now", "safety.violence.threat_now", None) in _matches(
        "он мне угрожает сейчас"
    )
    assert _matches("он мне не угрожает") == ()


@pytest.mark.parametrize(
    "case_id",
    (
        "psychologist-08",
        "psychologist-09",
        "multi-handoff-continue-open-01",
    ),
)
def test_open_path_fixture_rows_have_no_text_only_hard_route(case_id: str) -> None:
    assert _matches(_fixture_text(case_id)) == ()


def test_clear_conversation_request_exits_completed_aid_workflow() -> None:
    matches = _matches(_fixture_text("multi-aid-completion-open-01"))

    assert any(
        kind == "open_conversation_request" and rule_id == "conversation.continue.explicit"
        for kind, rule_id, _ in matches
    ), "multi-aid-completion-open-01 must retain its deterministic conversation exit"


def test_normalization_is_casefolded_punctuation_tolerant_and_normalizes_yo() -> None:
    plain = _matches("нужны вещи для ребенка")
    normalized = _matches("НУЖНЫ!!! вещи для РЕБЁНКА")

    assert plain == normalized == (
        ("concrete_aid", "aid.children.items", NeedKind.CHILDREN),
    )


def test_token_scanning_does_not_match_substrings() -> None:
    matches = _matches("В продуктовом магазине работает операторская служба")

    assert matches == ()


@pytest.mark.parametrize(
    "text",
    (
        "я не хочу покончить с собой",
        "меня сейчас не бьют",
        "сегодня меня не выгнали на улицу",
    ),
)
def test_negated_safety_statements_do_not_produce_hard_safety_signals(text: str) -> None:
    kinds = {kind for kind, _, _ in _matches(text)}

    assert not {
        "suicide_or_self_harm",
        "violence_or_threat_now",
        "urgent_shelter",
    }.intersection(kinds)


def test_explicit_bot_rejection_is_a_human_request_even_though_it_is_negated() -> None:
    assert _matches("не хочу общаться с ботом") == (
        ("explicit_human_request", "human.reject_bot", None),
    )


@pytest.mark.parametrize(
    "text",
    (
        "хочу поговорить со специалисткой",
        "хочу поговорить со специалистом",
        "хочу поговорить с человеком",
        "хочу поговорить с оператором",
    ),
)
def test_explicit_desire_to_speak_with_an_external_role_is_a_human_request(text: str) -> None:
    assert ("explicit_human_request", "human.want_talk.role", None) in _matches(text)


@pytest.mark.parametrize(
    "text",
    (
        "мне нужен человеческий разговор",
        "поговори со мной",
        "выслушай",
        "не хочу поговорить со специалисткой",
        "я не хочу поговорить с оператором",
    ),
)
def test_conversational_near_misses_are_not_human_handoffs(text: str) -> None:
    assert "explicit_human_request" not in {kind for kind, _, _ in _matches(text)}


def test_audit_model_contains_hash_and_offsets_but_never_raw_input() -> None:
    from app.signals import extract_signals

    text = "Позовите человека. private-audit-marker-7F3D"
    signals = extract_signals(text)
    dump = signals.model_dump(mode="json")

    assert dump["matcher_version"] == "deterministic-signals-v3"
    assert len(dump["input_hash"]) == 64
    assert dump["matches"] == [
        {
            "kind": "explicit_human_request",
            "rule_id": "human.transfer.role",
            "token_start": 0,
            "token_end": 2,
            "need": None,
        }
    ]
    assert "private-audit-marker-7F3D" not in repr(dump)


@pytest.mark.parametrize(
    ("text", "expected_kind", "expected_rule"),
    (
        ("расскажите, пожалуйста", "psychologist_considering", "psychologist.pending.explain"),
        ("да, хочу", "psychologist_request", "psychologist.pending.accept"),
    ),
)
def test_pending_psychologist_offer_authorizes_only_bounded_followup_signals(
    text: str, expected_kind: str, expected_rule: str
) -> None:
    from app.domain import SupportOffer
    from app.signals import extract_signals

    matches = extract_signals(text, pending_offer=SupportOffer.PSYCHOLOGIST)

    assert (expected_kind, expected_rule, None) in _matches(text, pending_offer=SupportOffer.PSYCHOLOGIST)
    assert matches.matches

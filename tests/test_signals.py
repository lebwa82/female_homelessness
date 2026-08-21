from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain import NeedKind

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dialogue_scenarios.jsonl"


def _fixture_text(case_id: str) -> str:
    for line in FIXTURE_PATH.read_text().splitlines():
        row = json.loads(line)
        if row["id"] == case_id:
            return next(text for role, text in reversed(row["history"]) if role == "user")
    raise AssertionError(f"missing fixture case: {case_id}")


def _matches(text: str) -> tuple[tuple[str, str, NeedKind | None], ...]:
    from app.signals import extract_signals

    return tuple((match.kind.value, match.rule_id, match.need) for match in extract_signals(text).matches)


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


def test_audit_model_contains_hash_and_offsets_but_never_raw_input() -> None:
    from app.signals import extract_signals

    text = "Позовите человека. private-audit-marker-7F3D"
    signals = extract_signals(text)
    dump = signals.model_dump(mode="json")

    assert dump["matcher_version"] == "deterministic-signals-v1"
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

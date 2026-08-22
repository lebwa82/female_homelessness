from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.dialogue_eval import DatasetError, load_cases

DATASET = Path(__file__).parent / "fixtures" / "dialogue_scenarios.jsonl"


def _valid_row(case_id: str) -> dict[str, object]:
    return {
        "version": 3,
        "id": case_id,
        "group": "open_conversation",
        "history": [["user", "анонимный текст"]],
        "initial": {"state": "open_conversation", "pending_offer": None},
        "expected": {
            "behavior": {
                "local_risk": "none",
                "choice_set": "none",
                "rendered_callback_ids": ["human"],
                "effect": "none",
                "side_effects": [],
                "state_after": "open_conversation",
                "escalation": False,
                "escalation_cause": None,
                "escalation_count": 0,
                "request_count": 0,
                "copy_contains": None,
                "rule_ids": [],
            },
            "diagnostics": {
                "safety_levels": ["none"],
                "support_intents": ["open_conversation"],
            },
        },
    }


def test_dataset_has_required_coverage() -> None:
    """Removing a required behavioural group or regression case must fail."""
    cases = load_cases(DATASET)

    assert len(cases) >= 48
    assert {case.id for case in cases} >= {
        "prod-listen-01",
        "human-01",
        "suicide-01",
        "psychologist-request-01",
    }
    assert {
        "open_conversation",
        "explicit_human",
        "human_near_miss",
        "aid_intent",
        "psychologist",
        "crisis",
        "multi_turn",
    } <= {case.group for case in cases}
    assert all(case.version == 3 for case in cases)
    assert {
        "psychologist-considering-01",
        "psychologist-request-01",
        "psychologist-02",
        "psychologist-03",
        "psychologist-04",
        "psychologist-05",
        "psychologist-06",
        "psychologist-07",
        "psychologist-08",
        "psychologist-09",
        "multi-psychologist-request-01",
        "soft-offer-consume",
        "soft-offer-expire",
    } <= {case.id for case in cases if case.initial.pending_offer is not None}


def test_dataset_ids_are_unique() -> None:
    """Accidentally shadowing a case ID must be rejected, not silently replayed."""
    duplicate = "\n".join(
        (
            json.dumps(_valid_row("duplicate-01")),
            json.dumps(_valid_row("duplicate-01")),
        )
    )

    with pytest.raises(DatasetError, match="duplicate case id: duplicate-01"):
        load_cases_from_text(duplicate)


def test_behavior_requires_explicit_rule_ids_and_canonical_copy_for_backend_routes() -> None:
    missing_rule_ids = _valid_row("missing-rule-ids")
    missing_rule_ids["expected"]["behavior"].pop("rule_ids")  # type: ignore[index]

    with pytest.raises(DatasetError, match="missing required keys: rule_ids"):
        load_cases_from_text(json.dumps(missing_rule_ids))

    missing_copy = _valid_row("missing-canonical-copy")
    behavior = missing_copy["expected"]["behavior"]  # type: ignore[index]
    behavior["rule_ids"] = []
    behavior["choice_set"] = "aid_catalog"
    behavior["effect"] = "offer_aid"

    with pytest.raises(DatasetError, match="canonical copy is required"):
        load_cases_from_text(json.dumps(missing_copy))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"id": "broken"}, "missing required keys"),
        ({**_valid_row("role-01"), "history": [["system", "no"]]}, "invalid history role"),
        ({**_valid_row("expectation-01"), "expected": {"behavior": {}}}, "missing required keys"),
    ],
)
def test_malformed_dataset_rows_fail_with_clear_errors(row: dict[str, object], message: str) -> None:
    """A malformed versioned row must not be mistaken for a valid evaluation."""
    with pytest.raises(DatasetError, match=message):
        load_cases_from_text(json.dumps(row))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("local_risk", "invented-risk"),
        ("choice_set", "invented-choice-set"),
        ("effect", "invented-effect"),
    ],
)
def test_expected_enum_values_must_belong_to_the_domain(
    field: str, value: object
) -> None:
    """Typos in symbolic invariants must fail at load time rather than at replay time."""
    row = _valid_row(f"invalid-{field}")
    row["expected"]["behavior"][field] = value  # type: ignore[index]

    with pytest.raises(DatasetError, match=f"expected.behavior.{field} contains invalid enum value"):
        load_cases_from_text(json.dumps(row))


def load_cases_from_text(text: str):  # type: ignore[no-untyped-def]
    path = Path(__file__).parent / "fixtures" / "_temporary_dialogue_cases.jsonl"
    path.write_text(text, encoding="utf-8")
    try:
        return load_cases(path)
    finally:
        path.unlink(missing_ok=True)

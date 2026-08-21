from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.dialogue_eval import DatasetError, load_cases

DATASET = Path(__file__).parent / "fixtures" / "dialogue_scenarios.jsonl"


def _valid_row(case_id: str) -> dict[str, object]:
    return {
        "id": case_id,
        "group": "open_conversation",
        "history": [["user", "анонимный текст"]],
        "expected": {
            "risk": ["none"],
            "intent": ["open_conversation"],
            "choice_set": "none",
            "effect": "none",
            "escalation": False,
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


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"id": "broken"}, "missing required keys"),
        ({**_valid_row("role-01"), "history": [["system", "no"]]}, "invalid history role"),
        ({**_valid_row("expectation-01"), "expected": {"risk": "none"}}, "expected.risk"),
    ],
)
def test_malformed_dataset_rows_fail_with_clear_errors(row: dict[str, object], message: str) -> None:
    """A malformed versioned row must not be mistaken for a valid evaluation."""
    with pytest.raises(DatasetError, match=message):
        load_cases_from_text(json.dumps(row))


def load_cases_from_text(text: str):  # type: ignore[no-untyped-def]
    path = Path(__file__).parent / "fixtures" / "_temporary_dialogue_cases.jsonl"
    path.write_text(text, encoding="utf-8")
    try:
        return load_cases(path)
    finally:
        path.unlink(missing_ok=True)

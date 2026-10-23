"""Aggregate pseudonymous contact features; never read dialogue content or raw contact IDs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

METRIC_VERSION = "help-funnel-v1"
EVENT_FIELDS = (
    "starts", "continues", "menu_shown", "menu_answers", "aid_selected",
    "certificate_delivered", "consultation_completed",
)
ROW_FIELDS = {"contact_key", "imported", "excluded", "message_count", *EVENT_FIELDS}
TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
)


class ReportInputError(ValueError):
    """A validation error whose message never contains an input value."""


@dataclass
class _Contact:
    imported: bool = False
    excluded: bool = False
    has_messages: bool = False
    events: dict[str, set[datetime]] = field(
        default_factory=lambda: {name: set() for name in EVENT_FIELDS}
    )


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise ReportInputError("invalid_timestamp")
    if value[-1] != "Z" and int(value[-2:]) >= 60:
        raise ReportInputError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ReportInputError("invalid_timestamp") from None


def _merge(rows: object) -> dict[str, _Contact]:
    if not isinstance(rows, list):
        raise ReportInputError("rows_must_be_a_list")
    contacts: dict[str, _Contact] = {}
    for row in rows:
        if not isinstance(row, dict) or row.keys() != ROW_FIELDS:
            raise ReportInputError("invalid_row_fields")
        key = row["contact_key"]
        if not isinstance(key, str) or not key.strip():
            raise ReportInputError("invalid_contact_key")
        if type(row["imported"]) is not bool or type(row["excluded"]) is not bool:
            raise ReportInputError("invalid_cohort_flags")
        if type(row["message_count"]) is not int or row["message_count"] < 0:
            raise ReportInputError("invalid_message_count")
        contact = contacts.setdefault(key, _Contact())
        contact.imported |= row["imported"]
        contact.excluded |= row["excluded"]
        contact.has_messages |= row["message_count"] > 0
        for name in EVENT_FIELDS:
            if not isinstance(row[name], list):
                raise ReportInputError("events_must_be_lists")
            contact.events[name].update(_timestamp(value) for value in row[name])
    return contacts


def _after(events: set[datetime], earlier: set[datetime]) -> set[datetime]:
    if not earlier:
        return set()
    first = min(earlier)
    return {event for event in events if event > first}


def _metric(denominator: set[int], numerator: set[int], total: int) -> dict:
    return {
        "denominator_contacts": len(denominator),
        "numerator_contacts": len(numerator),
        "conversion_pct": round(100 * len(numerator) / len(denominator), 2)
        if denominator else None,
        "unknown": {
            "outcome_contacts": len(denominator - numerator),
            "entry_contacts": total - len(denominator),
        },
    }


def _cohort(contacts: list[_Contact]) -> dict:
    starts, continued, answered, certificates, consultations = set(), set(), set(), set(), set()
    shown, selected, selected_after_answer = set(), set(), set()
    answers_without_menu, answers_before_menu, continues_without_start = set(), set(), set()
    for index, contact in enumerate(contacts):
        events = contact.events
        if events["starts"]:
            starts.add(index)
        if _after(events["continues"], events["starts"]):
            continued.add(index)
        elif events["continues"]:
            continues_without_start.add(index)
        if events["menu_shown"]:
            shown.add(index)
        if events["aid_selected"]:
            selected.add(index)
        answers = events["menu_answers"]
        if events["menu_shown"]:
            answers = _after(answers, events["menu_shown"])
            if events["menu_answers"] and not answers:
                answers_before_menu.add(index)
        elif answers:
            # A machine-recorded menu answer remains evidence when the menu
            # delivery event is missing from historical observations.
            answers_without_menu.add(index)
        if answers:
            answered.add(index)
        if _after(events["aid_selected"], answers):
            selected_after_answer.add(index)
        if _after(events["certificate_delivered"], answers):
            certificates.add(index)
        if _after(events["consultation_completed"], answers):
            consultations.add(index)
    helped = certificates | consultations
    h2 = _metric(starts, continued, len(contacts))
    h3 = _metric(answered, helped, len(contacts))
    h3.update({
        "certificate_after_answer_contacts": len(certificates),
        "consultation_after_answer_contacts": len(consultations),
        "both_outcomes_after_answer_contacts": len(certificates & consultations),
    })
    return {
        "contacts": len(contacts),
        "h2": h2,
        "h3": h3,
        "coverage": {
            "contacts_with_messages": sum(contact.has_messages for contact in contacts),
            "menu_shown_contacts": len(shown),
            "aid_selected_contacts": len(selected),
            "aid_selected_after_menu_answer_contacts": len(selected_after_answer),
            "menu_answers_without_observed_menu_contacts": len(answers_without_menu),
            "menu_answers_without_prior_observed_menu_contacts": len(answers_before_menu),
            "continues_without_prior_observed_start_contacts": len(continues_without_start),
        },
        "subset_checks": {
            "h2_numerator_within_denominator": continued <= starts,
            "h3_numerator_within_denominator": helped <= answered,
            "h3_outcome_union_matches_numerator": (
                len(helped) == len(certificates) + len(consultations)
                - len(certificates & consultations)
            ),
        },
    }


def build_report(rows: object) -> dict:
    """Return all-time observed conversions, not inferred refusals or causal effects.

    Rows with the same contact_key are merged. Any exclusion excludes the whole
    contact; any imported flag assigns that contact to the imported cohort.
    Empty event lists mean no evidence, not proof that an event did not happen.
    Event times must be timezone-aware ISO timestamps with seconds (up to six
    fractional digits). Equal timestamps do not establish an event ordering.
    """
    merged = _merge(rows)
    included = [contact for contact in merged.values() if not contact.excluded]
    cohorts = {
        "all": _cohort(included),
        "native": _cohort([contact for contact in included if not contact.imported]),
        "imported": _cohort([contact for contact in included if contact.imported]),
    }
    return {
        "metric_version": METRIC_VERSION,
        "unit": "unique_contact",
        "scope": "all_time",
        "measurement": "observed_strict",
        "unknown_meaning": "missing_observation_not_proven_refusal",
        "counts": {
            "input_rows": len(rows),
            "included_contacts": len(included),
            "excluded_contacts": sum(contact.excluded for contact in merged.values()),
        },
        "cohorts": cohorts,
        "subset_checks": {
            "cohorts_partition_all": cohorts["all"]["contacts"]
            == cohorts["native"]["contacts"] + cohorts["imported"]["contacts"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="JSON array of pseudonymous per-contact feature rows")
    args = parser.parse_args(argv)
    try:
        rows = json.loads(args.file.read_text(encoding="utf-8"))
        report = build_report(rows)
    except (OSError, UnicodeError, json.JSONDecodeError, ReportInputError):
        print("help_funnel: invalid or unreadable feature input", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

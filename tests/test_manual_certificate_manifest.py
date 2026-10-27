"""Synthetic evidence only: no customer records or network dependencies."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.chatwoot.manual_certificate_manifest import build_followup_manifest

GENERATED = datetime(2026, 10, 27, 12, tzinfo=UTC)


def source():
    return {"spreadsheet_id": "synthetic_sheet", "sheet_name": "Sheet1", "gid": 0,
            "snapshot_sha256": "a" * 64}


def rows(count=39):
    return [{
        "source_row": index + 2, "conversation_id": index + 100,
        "internal_conversation_id": index + 200, "contact_id": index + 300,
        "source_message_id": index + 400, "aid_id": "children_card",
        "provider_slug": "ozon", "issued_on": "2026-10-20",
        "source_message_created_at": "2026-10-20T08:00:00",
        "telegram_acceptance_verified": index != 38, "human_assigned": index == 0,
    } for index in range(count)]


def build(data=None, **kwargs):
    return build_followup_manifest(rows() if data is None else data, source(),
                                   generated_at=GENERATED, **kwargs)


def test_manifest_is_inert_complete_and_uses_stable_contact_keys():
    data = rows()
    before = deepcopy(data)
    manifest = build(data)
    assert data == before
    assert manifest["apply"] is False
    assert manifest["schema_version"] == 1
    assert manifest["account_id"] == 1
    assert manifest["generated_at"] == "2026-10-27T12:00:00+00:00"
    assert manifest["required_capabilities"] == [
        "historical_certificate_survey_copy", "recheck_existing_certificate_followups",
    ]
    assert len(manifest["jobs"]) == 39
    assert sum(job["telegram_acceptance_verified"] for job in manifest["jobs"]) == 38
    job = manifest["jobs"][0]
    assert job["job_key"] == "certificate:manual:1:300"
    assert job["currently_human_assigned"] is True
    assert job["source_date"] == "2026-10-20"
    assert job["source_message_created_at"] == "2026-10-20T08:00:00+00:00"
    assert job["source_ref"] == "sheet:synthetic_sheet:0:2"
    assert job["source_reference"]["url"] == (
        "https://docs.google.com/spreadsheets/d/synthetic_sheet/edit#gid=0&range=A2"
    )
    assert job["payload"] == {
        "screen": "s5", "state": "pending", "due_at": "2026-10-24T10:00:00+03:00",
        "context": {"aid_id": "children_card", "historical_survey": True},
    }
    assert build(list(reversed(data))) == manifest


def test_sheet_day_controls_schedule_not_source_message_timestamp():
    data = rows(1)
    data[0]["issued_on"] = "2026-09-29"
    data[0]["source_message_created_at"] = "2026-10-01T21:30:00+03:00"
    job = build(data, expected_count=1)["jobs"][0]
    assert job["payload"]["due_at"] == "2026-10-03T10:00:00+03:00"
    assert job["source_message_created_at"] == "2026-10-01T18:30:00+00:00"


def test_only_allowlisted_normalized_fields_are_retained():
    data = rows(1)
    data[0].update({"client_name": "synthetic private value", "raw_type": "freeform",
                    "code": "DO_NOT_RETAIN", "expires_at": "2030-01-01",
                    "expiry_date": "2030-01-01"})
    metadata = {**source(), "unneeded_private_metadata": "DO_NOT_RETAIN"}
    manifest = build_followup_manifest(data, metadata, generated_at=GENERATED, expected_count=1)
    assert manifest["source"] == source()
    text = repr(manifest)
    for excluded in ("client_name", "raw_type", "code", "expires_at", "expiry_date",
                     "DO_NOT_RETAIN", "synthetic private value"):
        assert excluded not in text


@pytest.mark.parametrize("field", [
    "source_row", "conversation_id", "internal_conversation_id", "contact_id",
    "source_message_id",
])
def test_unique_recipient_evidence_is_required(field):
    data = rows(2)
    data[1][field] = data[0][field]
    with pytest.raises(ValueError, match=f"duplicate {field}"):
        build(data, expected_count=2)


@pytest.mark.parametrize("field,value", [
    ("source_row", 1), ("conversation_id", True), ("contact_id", 0),
    ("internal_conversation_id", "200"), ("source_message_id", -1),
    ("aid_id", "unknown"), ("provider_slug", "unknown"),
    ("issued_on", "2026-02-30"), ("issued_on", "20.10.2026"),
    ("issued_on", "2026-10-27"), ("issued_on", "2026-10-28"),
    ("issued_on", "2026-10-24"),  # Its day-four survey is still in the future.
    ("source_message_created_at", "2026-10-20"),
    ("source_message_created_at", "invalid"),
    ("source_message_created_at", "2026-10-28T08:00:00"),
    ("telegram_acceptance_verified", 1), ("human_assigned", "false"),
])
def test_invalid_normalized_evidence_fails_closed(field, value):
    data = rows(1)
    data[0][field] = value
    with pytest.raises(ValueError):
        build(data, expected_count=1)


@pytest.mark.parametrize("field,value", [
    ("spreadsheet_id", ""), ("spreadsheet_id", "not/a/sheet"),
    ("sheet_name", "Other"), ("gid", 1), ("gid", False), ("gid", 0.0),
    ("snapshot_sha256", "not-a-digest"),
])
def test_source_identity_is_validated(field, value):
    metadata = {**source(), field: value}
    with pytest.raises(ValueError):
        build_followup_manifest(rows(), metadata, generated_at=GENERATED)


def test_exact_expected_count_and_aware_generation_time_are_required():
    with pytest.raises(ValueError, match="row count"):
        build(rows(38))
    for expected in (0, -1, True):
        with pytest.raises(ValueError, match="expected_count"):
            build(expected_count=expected)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_followup_manifest(rows(), source(), generated_at=GENERATED.replace(tzinfo=None))


def test_all_known_historical_aids_and_providers_are_accepted_independently():
    for aid in ("children_card", "food_card", "medicine_card", "hostel_3_nights"):
        for provider in ("ozon", "pyaterochka"):
            data = rows(1)
            data[0].update(aid_id=aid, provider_slug=provider)
            job = build(data, expected_count=1)["jobs"][0]
            assert (job["aid_id"], job["provider_slug"]) == (aid, provider)

from copy import deepcopy

import pytest

from scripts.backfill_certificate_history import candidates, timestamp


def report():
    return {"rows": [{
        "category": "explicit_waitlist",
        "delivery_audit": "manual_certificate_sent_to_telegram",
        "contact_id": 7,
        "first_confirmed_request": "2026-10-20 10:00:00",
        "manual_pdf_messages": [{
            "message_id": 41, "contact_id": 7, "conversation_id": 9,
            "created_at": "2026-10-20 11:00:00", "certificate_filename": True,
            "body_mentions_certificate": True, "external_receipt": True, "status": 0,
        }],
    }]}


def test_audit_selects_only_explicit_accepted_post_request_evidence():
    data = report()
    ambiguous = deepcopy(data["rows"][0])
    ambiguous["delivery_audit"] = "pdf_needs_review"
    data["rows"].append(ambiguous)
    assert [item["message_id"] for item in candidates(data, 1)] == [41]


@pytest.mark.parametrize("field,value", [
    ("external_receipt", False), ("status", 3), ("certificate_filename", False),
    ("body_mentions_certificate", False), ("contact_id", 8),
    ("created_at", "2026-10-20 09:00:00"),
])
def test_audit_refuses_insufficient_evidence(field, value):
    data = report()
    data["rows"][0]["manual_pdf_messages"][0][field] = value
    with pytest.raises(ValueError):
        candidates(data, 1)


def test_audit_requires_exact_unique_recipient_count():
    data = report()
    with pytest.raises(ValueError):
        candidates(data, 17)
    data["rows"].append(deepcopy(data["rows"][0]))
    with pytest.raises(ValueError):
        candidates(data, 2)


def test_chatwoot_naive_time_is_utc_not_local_time():
    assert timestamp("2026-10-20 11:00:00") == timestamp("2026-10-20T14:00:00+03:00")

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from analytics.dds.transform import entity_key, project

SECRET = b"x" * 32
NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


def scope(account=1):
    return SimpleNamespace(
        account_id=account, inbox_ids=(5,), excluded_contact_ids=(), label_titles=("legal",)
    )


def snapshot(**tables):
    return SimpleNamespace(
        tables=tables, complete_tables=set(tables), changed_conversation_ids={10}, captured_at=NOW
    )


def base(**extra):
    tables = {
        "contacts": [{"id": 1, "account_id": 1, "created_at": NOW}],
        "inboxes": [{"id": 5, "account_id": 1, "channel_type": "Channel::Telegram"}],
        "conversations": [
            {
                "id": 10,
                "account_id": 1,
                "contact_id": 1,
                "inbox_id": 5,
                "status": 2,
                "created_at": NOW,
            }
        ],
    }
    tables.update(extra)
    return snapshot(**tables)


def test_keys_are_stable_scoped_and_entity_specific():
    assert entity_key(SECRET, 1, "contact", 1) == entity_key(SECRET, 1, "contact", 1)
    assert entity_key(SECRET, 1, "contact", 1) != entity_key(SECRET, 2, "contact", 1)
    assert entity_key(SECRET, 1, "contact", 1) != entity_key(SECRET, 1, "staff", 1)
    with pytest.raises(ValueError):
        entity_key(b"short", 1, "contact", 1)


def test_message_is_metadata_only_and_naive_dates_are_utc():
    s = base(
        messages=[
            {
                "id": 20,
                "account_id": 1,
                "conversation_id": 10,
                "inbox_id": 5,
                "sender_type": "Contact",
                "sender_id": 1,
                "created_at": NOW.replace(tzinfo=None),
                "message_type": 0,
                "status": 0,
                "private": False,
                "content": "DO_NOT_COPY",
                "content_attributes": {"token": "DO_NOT_COPY"},
            }
        ]
    )
    p = project(s, scope(), SECRET)
    m = p.tables["fact_message"][0]
    assert m["created_at"] == NOW
    assert m["sender_key"] == p.tables["dim_contact"][0]["contact_key"]
    assert m["private"] is False
    assert "DO_NOT_COPY" not in repr(p)
    assert "content" not in m


def test_requests_are_scoped_to_parent_and_missing_dates_remain_null():
    s = base(
        conversations=[
            {
                "id": i,
                "account_id": 1,
                "contact_id": 1,
                "inbox_id": 5,
                "scenario_requests": {
                    "same": {
                        "state": "completed",
                        "aid_id": "legal_consultation",
                        "completed_at": NOW.isoformat(),
                    }
                },
            }
            for i in (10, 11)
        ]
    )
    p = project(s, scope(), SECRET)
    rs = p.tables["fact_consultation_request"]
    assert len(rs) == 2 and rs[0]["request_key"] != rs[1]["request_key"]
    assert all(r["requested_at"] is None for r in rs)
    assert all(r["state"] == "completed" for r in rs)
    assert all("help_received" not in r for r in rs)


def test_malformed_json_is_quarantined_without_payload():
    s = base(conversations=[{"id": 10, "scenario_requests": {"bad": {"notes": "DO_NOT_COPY"}}}])
    p = project(s, scope(), SECRET)
    assert not p.tables["fact_consultation_request"]
    assert any(i.code == "invalid_request_state" for i in p.issues)
    assert "DO_NOT_COPY" not in repr(p)


def test_followup_target_is_resolved_only_to_valid_request_in_same_conversation():
    s = base(
        conversations=[
            {
                "id": 10,
                "scenario_requests": {"r": {"state": "requested", "aid_id": "legal_consultation"}},
                "scenario_followups": {
                    "f1": {"state": "answered", "context": {"request_key": "r"}},
                    "f2": {"state": "sent", "context": {"request_key": "other"}},
                },
            }
        ]
    )
    p = project(s, scope(), SECRET)
    fs = p.tables["fact_followup"]
    assert fs[0]["request_key"] == p.tables["fact_consultation_request"][0]["request_key"]
    assert fs[0]["target_kind"] == "consultation" and fs[0]["sent_at"] is None
    assert fs[1]["request_key"] is None and fs[1]["target_kind"] == "unresolved"
    assert fs[1]["unresolved_target_key"] is not None


def test_certificate_preserves_direct_nulls_and_derives_links_through_send_message():
    s = base(
        messages=[{"id": 20, "conversation_id": 10}],
        women_help_pdf_certificates=[
            {
                "id": 30,
                "record_kind": "certificate",
                "status": "submitted",
                "chatwoot_message_id": 20,
                "nominal_rubles": 2000,
                "activation_code": "DO_NOT_COPY",
            }
        ],
    )
    p = project(s, scope(), SECRET)
    cert = p.tables["fact_certificate"][0]
    assert cert["contact_key"] is None and cert["conversation_key"] is None
    assert cert["resolved_contact_key"] == p.tables["dim_contact"][0]["contact_key"]
    assert cert["resolved_conversation_key"] == p.tables["fact_conversation"][0]["conversation_key"]
    assert cert["link_origin"] == "send_message"
    assert "DO_NOT_COPY" not in repr(p)


def test_ratings_invalid_dates_and_codes_are_not_coerced():
    s = base(
        csat_survey_responses=[{"id": 1, "rating": 8, "created_at": "not-a-date"}],
        messages=[{"id": 20, "message_type": True, "status": 99}],
    )
    p = project(s, scope(), SECRET)
    assert p.tables["fact_csat"][0]["rating"] is None
    assert p.tables["fact_csat"][0]["created_at"] is None
    assert p.tables["fact_message"][0]["message_type"] is None
    assert p.tables["fact_message"][0]["status"] is None
    assert len(p.issues) >= 4


def test_cross_account_and_excluded_contacts_are_not_projected():
    s = base(contacts=[{"id": 1, "account_id": 2}])
    assert not project(s, scope(), SECRET).tables["dim_contact"]
    sc = scope()
    sc.excluded_contact_ids = (1,)
    assert not project(base(), sc, SECRET).tables["dim_contact"]


def test_queue_events_deduplicate_without_adding_requests():
    s = base(
        women_help_certificate_requests=[
            {
                "id": 3,
                "status": "waiting",
                "reason": "out_of_stock",
                "source_event_keys": ["e", "e", "f", None],
            }
        ]
    )
    p = project(s, scope(), SECRET)
    assert len(p.tables["fact_certificate_request"]) == 1
    assert len(p.tables["bridge_certificate_request_event"]) == 2
    assert "e" not in [str(x["event_key"]) for x in p.tables["bridge_certificate_request_event"]]


def test_tag_assignments_are_canonical_by_object_type_id_tag_and_context():
    s = base(
        labels=[{"id": 1, "title": "legal"}],
        tags=[{"id": 2, "name": "legal"}],
        taggings=[
            {
                "id": i,
                "tag_id": 2,
                "taggable_type": "Conversation",
                "taggable_id": 10,
                "context": "labels",
                "created_at": NOW,
            }
            for i in (4, 5)
        ],
    )
    p = project(s, scope(), SECRET)
    assert len(p.tables["bridge_object_tag"]) == 1
    assert len(p.tables["bridge_label_tag"]) == 1


def test_only_approved_category_values_are_retained():
    s = base(labels=[{"id": 1, "title": "DO_NOT_COPY"}], tags=[{"id": 2, "name": "DO_NOT_COPY"}])
    p = project(s, scope(), SECRET)
    assert not p.tables["dim_label"] and not p.tables["dim_tag"]
    assert "DO_NOT_COPY" not in repr(p)


def test_duplicate_source_id_uses_latest_version_and_reports_conflict():
    s = base(
        contacts=[{"id": 1, "updated_at": NOW}, {"id": 1, "updated_at": NOW, "created_at": NOW}]
    )
    p = project(s, scope(), SECRET)
    assert len(p.tables["dim_contact"]) == 1
    assert any(i.code == "duplicate_conflict" for i in p.issues)


@pytest.mark.parametrize("bad", [[], {}, {"unexpected": 1}])
def test_unhashable_scalar_values_are_quarantined(bad):
    s = base(
        messages=[{"id": 20, "status": bad, "sender_type": bad}],
        conversations=[
            {
                "id": 10,
                "scenario_requests": {"r": {"state": bad}},
                "scenario_followups": {"f": {"state": bad}},
            }
        ],
        users=[{"id": 2, "type": bad}],
        women_help_pdf_certificates=[{"id": 30, "provider": bad, "status": bad}],
    )
    p = project(s, scope(), SECRET)
    assert p.issues


def test_expired_followup_is_a_known_state():
    p = project(
        base(
            conversations=[
                {"id": 10, "scenario_followups": {"f": {"state": "expired", "context": {}}}}
            ]
        ),
        scope(),
        SECRET,
    )
    assert p.tables["fact_followup"][0]["state"] == "expired"


@pytest.mark.parametrize("ref", [[], {}, "", "text with spaces"])
def test_malformed_present_followup_link_is_unresolved_not_absent(ref):
    p = project(
        base(
            conversations=[
                {
                    "id": 10,
                    "scenario_followups": {
                        "f": {"state": "pending", "context": {"request_key": ref}}
                    },
                }
            ]
        ),
        scope(),
        SECRET,
    )
    f = p.tables["fact_followup"][0]
    assert f["target_kind"] == "unresolved" and f["unresolved_target_key"] is None
    assert any(i.code == "unresolved_target" for i in p.issues)


def test_certificate_enrichment_is_linear_in_source_record_count(monkeypatch):
    from analytics.dds import transform

    calls = 0
    original = transform.entity_key

    def count(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(transform, "entity_key", count)
    p = project(
        base(
            women_help_pdf_certificates=[
                {"id": i, "record_kind": "certificate", "status": "available"}
                for i in range(1, 201)
            ]
        ),
        scope(),
        SECRET,
    )
    assert len(p.tables["fact_certificate"]) == 200
    assert calls < 1200


def test_partial_queue_events_are_masked_and_invalid_events_reported():
    p = project(
        base(
            women_help_certificate_requests=[
                {"id": 3, "source_event_keys_scope": "partial", "source_event_keys": ["outside"]},
                {"id": 4, "source_event_keys_scope": "invalid", "source_event_keys": []},
            ]
        ),
        scope(),
        SECRET,
    )
    assert not p.tables["bridge_certificate_request_event"]
    assert {x.code for x in p.issues} >= {"partial_scope_events", "invalid_event_list"}


def test_invalid_temporal_order_is_flagged_without_inventing_dates():
    earlier = NOW.replace(hour=11)
    p = project(
        base(
            conversations=[
                {
                    "id": 10,
                    "scenario_requests": {
                        "r": {
                            "state": "completed",
                            "aid_id": "legal_consultation",
                            "requested_at": NOW,
                            "completed_at": earlier,
                        }
                    },
                }
            ],
            women_help_pdf_certificates=[{"id": 30, "valid_from": NOW, "expires_at": earlier}],
            women_help_certificate_requests=[
                {"id": 3, "first_requested_at": NOW, "last_requested_at": earlier}
            ],
        ),
        scope(),
        SECRET,
    )
    assert p.tables["fact_consultation_request"][0]["completed_at"] == earlier
    assert {i.code for i in p.issues} >= {
        "completed_before_requested",
        "expiry_before_validity",
        "last_before_first_request",
    }

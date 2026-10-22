"""Neutral contracts for native conversation-update bookkeeping deltas."""

from __future__ import annotations

import pytest

from app.chatwoot.contracts import ConversationChanged, parse_conversation_changed


def _delta(previous: object, current: object) -> dict[str, object]:
    return {"previous_value": previous, "current_value": current}


def _event(previous: object, current: object) -> dict[str, object]:
    # BaseListener#extract_changed_attributes serializes ActiveRecord
    # previous_changes into an array of single-key objects with full values.
    return {
        "event": "conversation_updated",
        "id": 23,
        "changed_attributes": [{"custom_attributes": _delta(previous, current)}],
    }


@pytest.mark.parametrize(
    "name",
    [
        "workflow_state",
        "workflow_need",
        "pending_aid_id",
        "pending_contact_method",
        "pending_city",
        "pending_district",
        "pending_offer",
        "workflow_navigation",
        "scenario",
        "scenario_pending_input",
        "scenario_last_message_id",
        "scenario_event_receipts",
        "scenario_followups",
        "bot_last_diagnostics",
        "bot_last_decision",
    ],
)
def test_ignores_only_known_bot_bookkeeping_attributes(name: str) -> None:
    previous = {"reply_owner": "bot", "routing_team_id": 5, name: "before"}
    current = {**previous, name: "after"}

    assert parse_conversation_changed(_event(previous, current)) is None


@pytest.mark.parametrize(
    "previous,current",
    [
        ({}, {"scenario": {"screen": "s2"}}),
        ({"scenario_pending_input": {"message_id": 41}}, {}),
        ({"scenario_pending_input": {}}, {"scenario_pending_input": None}),
        (
            {"workflow_state": "greeting"},
            {"workflow_state": "open_conversation", "scenario_last_message_id": 41},
        ),
    ],
)
def test_ignores_bookkeeping_additions_removals_and_combined_writes(previous, current) -> None:
    assert parse_conversation_changed(_event(previous, current)) is None


def test_allows_native_updated_at_alongside_bookkeeping() -> None:
    payload = _event({}, {"scenario_last_message_id": 41})
    payload["changed_attributes"].append(
        {
            "updated_at": _delta(
                "2026-10-22T10:00:00.000Z",
                "2026-10-22T10:00:01.000Z",
            )
        }
    )

    assert parse_conversation_changed(payload) is None


@pytest.mark.parametrize(
    "name",
    [
        "reply_owner",
        "ownership_version",
        "ownership_last_staff_message_id",
        "reopen_with_bot",
        "routing_team_id",
        "routing_origin",
        "routing_notice",
        "routing_assignment_pending",
        "routing_previous_team_id",
        "routing_message_id",
        "routing_last_decision",
        "context_epoch",
        "last_clear_message_id",
        "scenario_requests",
        "consultation_reviewed",
        "consultation_schedule_last_message_id",
        "unknown_attribute",
    ],
)
@pytest.mark.parametrize("kind", ["added", "changed", "removed"])
def test_preserves_human_control_routing_and_unknown_attribute_changes(
    name: str, kind: str
) -> None:
    previous = {"scenario_last_message_id": 40}
    current = {"scenario_last_message_id": 41}
    if kind != "added":
        previous[name] = "before"
    if kind != "removed":
        current[name] = "after"

    assert parse_conversation_changed(_event(previous, current)) == ConversationChanged(23)


@pytest.mark.parametrize(
    "name",
    [
        "status",
        "assignee_id",
        "assignee_type",
        "team_id",
        "assignee_team_id",
        "inbox_id",
        "priority",
        "unknown_native_field",
    ],
)
def test_preserves_bookkeeping_mixed_with_native_assignment_or_other_changes(name: str) -> None:
    payload = _event({}, {"scenario_last_message_id": 41})
    payload["changed_attributes"].append({name: _delta(1, 2)})

    assert parse_conversation_changed(payload) == ConversationChanged(23)


@pytest.mark.parametrize(
    "previous,current",
    [
        (True, 1),
        (1, 1.0),
        ({"nested": True}, {"nested": 1}),
        ([True], [1]),
        (None, "missing"),
    ],
)
def test_unknown_attribute_diff_is_type_sensitive(previous, current) -> None:
    payload = _event(
        {"unknown": previous, "scenario_last_message_id": 40},
        {"unknown": current, "scenario_last_message_id": 41},
    )

    assert parse_conversation_changed(payload) == ConversationChanged(23)


def test_object_key_order_is_not_a_protected_attribute_change() -> None:
    payload = _event(
        {"routing_notice": {"team_id": 5, "sent": True}, "scenario_last_message_id": 40},
        {"routing_notice": {"sent": True, "team_id": 5}, "scenario_last_message_id": 41},
    )

    assert parse_conversation_changed(payload) is None


@pytest.mark.parametrize(
    "changes",
    [
        None,
        [],
        {},
        "custom_attributes",
        [{}, {}],
        ["custom_attributes"],
        [{"custom_attributes": {}}],
        [{"custom_attributes": {"previous_value": {}}}],
        [{"custom_attributes": {"current_value": {"scenario_last_message_id": 41}}}],
        [{"custom_attributes": {**_delta({}, {"scenario_last_message_id": 41}), "unknown": 1}}],
        [
            {
                "custom_attributes": _delta({}, {"scenario_last_message_id": 41}),
                "updated_at": _delta("before", "after"),
            }
        ],
        [{"custom_attributes": _delta({}, {"scenario_last_message_id": 41})}] * 2,
        [{"updated_at": _delta("2026-10-22T10:00:00Z", "2026-10-22T10:00:01Z")}],
        [{"custom_attributes": _delta(None, {"scenario_last_message_id": 41})}],
        [{"custom_attributes": _delta({}, ["scenario_last_message_id"])}],
        [{"custom_attributes": _delta({}, {1: "not-a-json-key"})}],
        [{"custom_attributes": _delta({}, {"scenario_last_message_id": float("nan")})}],
        [{"custom_attributes": _delta({}, {"scenario_last_message_id": object()})}],
        [{"custom_attributes": _delta({}, {})}],
    ],
)
def test_preserves_unknown_or_malformed_native_delta_shapes(changes: object) -> None:
    payload = {"event": "conversation_updated", "id": 23, "changed_attributes": changes}

    assert parse_conversation_changed(payload) == ConversationChanged(23)


@pytest.mark.parametrize(
    "delta",
    [
        None,
        {},
        _delta(None, None),
        _delta(1, 2),
        _delta("before", "after"),
        _delta("2026-10-22", "2026-10-23"),
        _delta("2026-10-22T10:00:00Z", "not-a-timestamp"),
        {**_delta("2026-10-22T10:00:00Z", "2026-10-22T10:00:01Z"), "unknown": 1},
    ],
)
def test_preserves_malformed_updated_at_changes(delta: object) -> None:
    payload = _event({}, {"scenario_last_message_id": 41})
    payload["changed_attributes"].append({"updated_at": delta})

    assert parse_conversation_changed(payload) == ConversationChanged(23)


def test_never_filters_status_change_event_even_with_bookkeeping_only_delta() -> None:
    payload = {
        **_event({}, {"scenario_last_message_id": 41}),
        "event": "conversation_status_changed",
    }

    assert parse_conversation_changed(payload) == ConversationChanged(23)


@pytest.mark.parametrize("event", ["conversation_updated", "conversation_status_changed"])
def test_preserves_legacy_events_without_changed_attributes(event: str) -> None:
    assert parse_conversation_changed({"event": event, "id": 23}) == ConversationChanged(23)

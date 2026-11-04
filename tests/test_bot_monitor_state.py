import copy
import json

import pytest

from app.monitoring.state import CHECKS, REASONS, format_telegram_event, transition


def observations(**overrides):
    result = {check: {"ok": True, "reason": "ok"} for check in CHECKS}
    for check, reason in overrides.items():
        result[check] = {"ok": False, "reason": reason}
    return result


@pytest.mark.parametrize("check", CHECKS)
def test_failure_thresholds_and_cold_start_never_false_green(check):
    bad = observations(**{check: "probe_failed"})
    state, events, reported = transition({}, bad, 100)
    immediate = check in {"telegram_poll", "host"}
    assert reported[check]["ok"] is (False if immediate else None)
    assert [event["kind"] for event in events] == (["down"] if immediate else [])
    state, events, reported = transition(state, bad, 160)
    assert reported[check]["ok"] is False
    if immediate:
        assert events == []
    else:
        assert len(events) == 1
        assert events[0]["kind"] == "down"
        assert events[0]["started_at"] == 100
        assert events[0]["duration_seconds"] == 60


def test_healthy_start_and_isolated_failures_do_not_create_incidents():
    state, events, reported = transition({}, observations(), 0)
    assert events == []
    assert all(item["ok"] is True for item in reported.values())
    state, events, reported = transition(state, observations(agent="agent_unavailable"), 60)
    assert events == []
    assert reported["agent"] == {"ok": True, "reason": "ok"}
    state, events, reported = transition(state, observations(), 120)
    assert events == []
    state, events, reported = transition(state, observations(agent="agent_unavailable"), 180)
    assert events == []
    assert reported["agent"]["ok"] is True


def test_initial_success_after_cold_pending_failure_is_healthy_without_recovery():
    state, _, _ = transition({}, observations(queues="queue_stalled"), 10)
    _, events, reported = transition(state, observations(), 20)
    assert reported["queues"]["ok"] is True
    assert events == []


def test_recovery_needs_two_consecutive_successes_and_reuses_incident_id():
    bad = observations(host="container_stopped")
    state, events, _ = transition({}, bad, 10)
    incident_id = events[0]["event_id"]
    state, events, reported = transition(state, observations(), 20)
    assert events == []
    assert reported["host"]["ok"] is False
    state, events, _ = transition(state, bad, 30)
    assert events == []
    state, events, reported = transition(state, observations(), 40)
    assert events == []
    assert reported["host"]["ok"] is False
    state, events, reported = transition(state, observations(), 50)
    assert events == [
        {
            "check": "host",
            "event_id": incident_id,
            "kind": "recovery",
            "reason": "ok",
            "started_at": 10,
            "at": 50,
            "duration_seconds": 40,
        }
    ]
    assert reported["host"]["ok"] is True
    state, events, _ = transition(state, observations(), 60)
    assert events == []
    _, events, _ = transition(state, bad, 70)
    assert events[0]["event_id"] != incident_id


def test_reminder_boundary_and_json_restart_preserve_incident():
    bad = observations(telegram_proxy="proxy_unavailable")
    state, _, _ = transition({}, bad, 0)
    state = json.loads(json.dumps(state))
    state, events, _ = transition(state, bad, 60)
    incident_id = events[0]["event_id"]
    state, events, _ = transition(state, bad, 1859)
    assert events == []
    state = json.loads(json.dumps(state))
    state, events, _ = transition(state, bad, 1860)
    assert len(events) == 1
    assert events[0]["kind"] == "reminder"
    assert events[0]["event_id"] == incident_id
    assert events[0]["duration_seconds"] == 1860
    state, events, _ = transition(state, bad, 1860)
    assert events == []
    state, events, _ = transition(state, observations(), 1900)
    assert events == []
    _, events, _ = transition(json.loads(json.dumps(state)), observations(), 1960)
    assert events[0]["kind"] == "recovery"
    assert events[0]["event_id"] == incident_id


def test_partial_or_invalid_observations_fail_closed_and_do_not_copy_payloads():
    supplied = {
        "telegram_poll": {"ok": "yes", "reason": "ok", "secret": "private"},
        "telegram_proxy": {"ok": False, "reason": "private user payload"},
        "agent": {"ok": True, "reason": "unrecognized"},
        "queues": {"ok": True},
    }
    state, events, reported = transition({}, supplied, 10)
    assert all(item["reason"] == "observation_failed" for item in reported.values())
    assert reported["host"]["ok"] is False
    assert reported["agent"]["ok"] is None
    _, events, reported = transition(state, supplied, 20)
    assert all(item["ok"] is False for item in reported.values())
    assert "private" not in json.dumps((state, events, reported))
    assert all(
        set(event)
        == {
            "check",
            "event_id",
            "kind",
            "reason",
            "started_at",
            "at",
            "duration_seconds",
        }
        for event in events
    )


def test_transition_is_pure_and_deterministic():
    state, _, _ = transition({}, observations(), 10)
    original = copy.deepcopy(state)
    bad = observations(telegram_poll="poll_stale")
    assert transition(state, bad, 20) == transition(state, bad, 20)
    assert state == original


@pytest.mark.parametrize(
    "bad_state",
    [
        None,
        [],
        {"version": 1},
        {"version": 2, "updated_at": 1, "checks": {}},
    ],
)
def test_invalid_state_is_rejected_instead_of_reset(bad_state):
    with pytest.raises(ValueError):
        transition(bad_state, observations(), 100)


@pytest.mark.parametrize(
    "damage",
    [
        lambda state: state["checks"].pop("agent"),
        lambda state: state["checks"]["host"].update(ok=False),
        lambda state: state["checks"]["agent"].update(failures=-1),
        lambda state: state["checks"]["agent"].update(reason="secret"),
        lambda state: state["checks"]["agent"].update(pending_since=500),
        lambda state: state.update(updated_at=float("nan")),
    ],
)
def test_corrupt_nested_state_is_rejected(damage):
    state, _, _ = transition({}, observations(), 10)
    damage(state)
    with pytest.raises(ValueError):
        transition(state, observations(), 100)


@pytest.mark.parametrize("now", [-1, True, float("nan"), float("inf"), "100"])
def test_invalid_current_time_is_rejected(now):
    with pytest.raises(ValueError):
        transition({}, observations(), now)


def test_backward_time_is_rejected():
    state, _, _ = transition({}, observations(), 100)
    with pytest.raises(ValueError):
        transition(state, observations(), 99)


def test_telegram_event_is_concise_plain_text_with_bounded_content():
    _, events, _ = transition({}, observations(host="container_stopped"), 10)
    text = format_telegram_event(events[0], "https://status.example/a?x=1&y=<x>")
    assert text == (
        "Сбой: Сервер\nПричина: Контейнер остановлен\nНачало: 01.01.1970 03:00 МСК\n"
        "Длительность: 0 с\n"
        "https://status.example/a?x=1&y=<x>"
    )
    with pytest.raises(ValueError):
        format_telegram_event(
            {**events[0], "reason": "private user payload"}, "https://status.example"
        )


def test_telegram_start_time_is_fixed_moscow_time_across_date_boundary():
    _, events, _ = transition({}, observations(host="container_stopped"), 86399)
    assert "Начало: 02.01.1970 02:59 МСК" in format_telegram_event(
        events[0], "https://status.example"
    )


@pytest.mark.parametrize("reason", sorted(REASONS))
def test_all_bounded_telegram_reasons_have_human_russian_labels(reason):
    _, events, _ = transition({}, observations(host="container_stopped"), 10)
    text = format_telegram_event({**events[0], "reason": reason}, "https://status.example")
    reason_line = text.splitlines()[1]
    assert reason_line.startswith("Причина: ")
    assert any("А" <= letter <= "я" for letter in reason_line.removeprefix("Причина: "))
    assert reason not in reason_line


@pytest.mark.parametrize(
    "fields",
    [
        {"started_at": None},
        {"started_at": True},
        {"started_at": float("nan")},
        {"started_at": -1},
        {"at": None},
        {"at": float("inf")},
        {"at": 9},
        {"duration_seconds": 1},
        {"started_at": 1e20, "at": 1e20},
    ],
)
def test_telegram_formatter_rejects_invalid_event_times(fields):
    _, events, _ = transition({}, observations(host="container_stopped"), 10)
    with pytest.raises(ValueError):
        format_telegram_event({**events[0], **fields}, "https://status.example")

from datetime import UTC, datetime, timedelta

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation

from app.chatwoot.contracts import (
    CONSULTATION_CANCEL,
    CONSULTATION_SCHEDULE,
    ConversationChanged,
    StaffMessage,
    parse_staff_message,
)
from app.chatwoot.scenario_effects import daytime
from app.chatwoot.service import ChatwootAgentService
from app.config import settings

NOW = datetime(2026, 10, 3, 10, tzinfo=UTC)
KEY = "a" * 24


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    api = FakeChatwoot()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9)
    service._scenario_effects.clock = lambda: NOW
    return api, service


def attributes(api):
    return api.conversation["custom_attributes"]


def jobs(api):
    return attributes(api).get("scenario_followups", {})


async def plan(api, service, value="04.10.2026 15:00", message_id=70):
    attributes(api).setdefault("scenario_requests", {
        KEY: {"aid_id": "legal_consultation", "state": "requested"},
    })
    attributes(api)["consultation_ends_at"] = value
    await service.process(StaffMessage(message_id, 23, 4, consultation_schedule="schedule"))


async def menu(api, service):
    await service.process(event("/start", 42))
    await service.process(event("continue", 43))
    assert attributes(api)["scenario"]["screen"] == "s2"
    assert jobs(api)["idle:0"]["due_at"] == (NOW + timedelta(hours=1)).isoformat()


@pytest.mark.parametrize("command,action", [(CONSULTATION_SCHEDULE, "schedule"),
                                            (CONSULTATION_CANCEL, "cancel")])
def test_schedule_only_private_authenticated_staff(command, action):
    payload = {"event": "message_created", "message_type": "outgoing", "private": True,
               "id": 70, "conversation": {"id": 23}, "sender": {"id": 4, "type": "user"},
               "content": command}
    assert parse_staff_message(payload).consultation_schedule == action
    assert parse_staff_message({**payload, "private": False}).consultation_schedule is None
    for kind in ("agent_bot", "contact"):
        assert parse_staff_message({**payload, "sender": {"id": 4, "type": kind}}) is None
    assert parse_staff_message({**payload, "message_type": "incoming"}) is None


@pytest.mark.asyncio
async def test_future_end_not_chat_close_schedules_once_across_restart(runtime):
    api, service = runtime
    await api.assign_human(23, 4)
    await plan(api, service)
    assert api.conversation["assignee_id"] == 4  # Scheduling is not a takeover or release.
    request = attributes(api)["scenario_requests"][KEY]
    assert request["state"] == "requested"
    assert jobs(api)[f"review:{KEY}"]["due_at"] == "2026-10-04T14:00:00+00:00"
    await api.set_status(23, "resolved")
    await service.process(ConversationChanged(23))
    assert attributes(api)["scenario_requests"][KEY] == request
    assert not await service.send_due_followup(23)
    restarted = ChatwootAgentService(api)
    restarted._scenario_effects.clock = lambda: NOW + timedelta(days=1, hours=4)
    assert await restarted.send_due_followup(23)
    assert not await restarted.send_due_followup(23)
    assert not any(k.startswith("reminder:review:") for k in jobs(api))
    await plan(api, service, "05.10.2026 15:00", 72)
    assert jobs(api)[f"review:{KEY}"]["state"] == "sent"
    assert attributes(api)["scenario_requests"][KEY]["state"] == "requested"


@pytest.mark.asyncio
async def test_reschedule_cancel_retry_and_late_old_macro(runtime):
    api, service = runtime
    await plan(api, service)
    await plan(api, service, "05.10.2026 19:30", 71)
    assert jobs(api)[f"review:{KEY}"]["due_at"] == "2026-10-06T07:00:00+00:00"
    await service.process(StaffMessage(72, 23, 4, consultation_schedule="cancel"))
    await plan(api, service, message_id=71)  # Redelivery cannot resurrect a cancelled timer.
    assert jobs(api)[f"review:{KEY}"]["state"] == "cancelled"
    service._scenario_effects.clock = lambda: NOW + timedelta(days=10)
    assert not await service.send_due_followup(23)
    await plan(api, service, "16.10.2026 15:00", 73)
    await plan(api, service, "20.10.2026 15:00", 73)  # Same event reads no changed fields.
    assert jobs(api)[f"review:{KEY}"]["due_at"] == "2026-10-16T14:00:00+00:00"
    assert len(jobs(api)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "tomorrow", "31.02.2026 15:00", "04.10.2026", "04.10.2026 29:00"])
async def test_invalid_date_does_not_change_existing_plan(runtime, value):
    api, service = runtime
    await plan(api, service)
    expected = jobs(api).copy()
    await plan(api, service, value, 71)
    assert jobs(api) == expected
    assert len(api.notes) == 2


@pytest.mark.asyncio
async def test_multiple_requests_require_id_and_missing_id_never_falls_back(runtime):
    api, service = runtime
    attributes(api)["scenario_requests"] = {
        k * 24: {"aid_id": "legal_consultation", "state": "requested"} for k in ("a", "b")
    }
    await plan(api, service)
    assert not jobs(api)
    attributes(api)["consultation_request_id"] = "c" * 24
    await plan(api, service, message_id=71)
    assert not jobs(api)
    attributes(api)["consultation_request_id"] = KEY
    await plan(api, service, message_id=72)
    assert list(jobs(api)) == [f"review:{KEY}"]


@pytest.mark.asyncio
async def test_completion_uses_actual_end_and_not_preplanned_date(runtime):
    api, service = runtime
    await plan(api, service)
    await service.process(StaffMessage(71, 23, 4, consultation_completed=True))
    assert jobs(api)[f"review:{KEY}"]["due_at"] == (NOW + timedelta(hours=2)).isoformat()
    assert attributes(api)["scenario_requests"][KEY]["state"] == "completed"


@pytest.mark.asyncio
async def test_closed_chat_does_not_keep_old_optional_form_blocking_meeting_review(runtime):
    api, service = runtime
    await plan(api, service)
    attributes(api)["scenario"] = {"screen": "s35d", "awaiting_text": True}
    service._scenario_effects.clock = lambda: NOW + timedelta(days=1, hours=4)
    assert not await service.send_due_followup(23)  # Do not interrupt an active form.
    await api.set_status(23, "resolved")
    assert await service.send_due_followup(23)  # That old form is no longer active.
    assert attributes(api)["scenario"]["screen"] == "s7"


@pytest.mark.asyncio
async def test_review_preempts_same_request_contact_check_not_other_requests(runtime):
    api, service = runtime
    await plan(api, service)
    for key in (f"check:{KEY}", f"reminder:check:{KEY}", "check:another"):
        jobs(api)[key] = {"screen": "s5", "state": "pending", "due_at": NOW.isoformat(),
                          "context": {"aid_id": "legal_consultation", "request_key": KEY}}
    service._scenario_effects.clock = lambda: NOW + timedelta(days=1, hours=4)
    assert await service.send_due_followup(23)
    assert attributes(api)["scenario"]["screen"] == "s7"
    assert jobs(api)[f"check:{KEY}"]["state"] == "cancelled"
    assert jobs(api)[f"reminder:check:{KEY}"]["state"] == "cancelled"
    assert jobs(api)["check:another"]["state"] == "pending"


@pytest.mark.asyncio
async def test_schedule_does_not_steal_or_silence_incoming_turn(runtime):
    api, service = runtime
    await plan(api, service)
    api.messages += ({"id": 70, "message_type": "outgoing", "private": True,
                      "sender": {"id": 4, "type": "user"}, "content": CONSULTATION_SCHEDULE},)
    assert await service.process(event("Hello", 71))
    assert attributes(api)["reply_owner"] == "bot"


@pytest.mark.asyncio
async def test_retry_after_note_failure_does_not_reread_edited_date(runtime, monkeypatch):
    api, service = runtime
    original = api.add_private_note

    async def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic_note_failure")

    monkeypatch.setattr(api, "add_private_note", unavailable)
    with pytest.raises(ConnectionError):
        await plan(api, service)
    expected = jobs(api).copy()
    monkeypatch.setattr(api, "add_private_note", original)
    await plan(api, service, "20.10.2026 15:00")
    assert jobs(api) == expected
    assert len(api.notes) == 1


@pytest.mark.asyncio
async def test_lost_review_ack_cannot_be_rescheduled_or_cancelled(runtime):
    api, service = runtime
    await plan(api, service)
    api.replies.append({"turn_key": f"scenario-followup:review:{KEY}"})
    before = jobs(api).copy()
    await plan(api, service, "20.10.2026 15:00", 71)
    await service.process(StaffMessage(72, 23, 4, consultation_schedule="cancel"))
    assert jobs(api) == before


@pytest.mark.asyncio
async def test_idle_one_hour_once_restart_and_back_do_not_rearm(runtime):
    api, service = runtime
    await menu(api, service)
    assert not await service.send_due_followup(23)
    restarted = ChatwootAgentService(api)
    restarted._scenario_effects.clock = lambda: NOW + timedelta(hours=1)
    assert await restarted.send_due_followup(23)
    assert attributes(api)["scenario"]["screen"] == "i4"
    assert not await restarted.send_due_followup(23)
    attributes(api)["scenario"] = {"screen": "s2"}  # Returning to the menu is not a new context.
    await restarted._scenario_effects.menu_delivered(23, 45)
    assert jobs(api)["idle:0"]["state"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("input_text", ["Hello", "/clear", "/system_info", "need:documents", "human"])
async def test_any_new_input_cancels_idle(runtime, input_text):
    api, service = runtime
    await menu(api, service)
    await service.process(event(input_text, 44))
    assert jobs(api)["idle:0"]["state"] == "cancelled"
    service._scenario_effects.clock = lambda: NOW + timedelta(hours=1)
    assert not await service.send_due_followup(23)


@pytest.mark.asyncio
async def test_clear_allows_one_new_menu_timer(runtime):
    api, service = runtime
    await menu(api, service)
    await service.process(event("/clear", 44))
    await service.process(event("/start", 45))
    await service.process(event("continue", 46))
    assert jobs(api)["idle:0"]["state"] == "cancelled"
    assert jobs(api)["idle:1"]["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("takeover", ["assignment", "public_reply", "queued_input", "queued_staff",
                                     "resolved", "snoozed"])
async def test_idle_never_follows_activity_or_human_takeover(runtime, takeover):
    api, service = runtime
    await menu(api, service)
    if takeover == "assignment":
        await api.assign_human(23, 4)
    elif takeover == "public_reply":
        await service.process(StaffMessage(44, 23, 4))
    elif takeover in {"resolved", "snoozed"}:
        await api.set_status(23, takeover)
    else:
        api.messages += ({"id": 44, "message_type": "incoming" if takeover == "queued_input" else "outgoing",
                          "sender": {"type": "contact" if takeover == "queued_input" else "user"},
                          "private": False},)
    service._scenario_effects.clock = lambda: NOW + timedelta(hours=1)
    assert not await service.send_due_followup(23)
    assert jobs(api)["idle:0"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_lost_menu_ack_reconciles_timer_without_repeat_reply(runtime):
    api, service = runtime
    await menu(api, service)
    attributes(api)["scenario_followups"] = {}
    attributes(api)["scenario_pending_input"] = {"message_id": 43}
    assert not await service.process(event("continue", 43))
    assert jobs(api)["idle:0"]["state"] == "pending"
    assert attributes(api)["scenario_pending_input"] is None


@pytest.mark.asyncio
async def test_idle_daytime_window_and_lost_survey_ack(runtime):
    api, service = runtime
    service._scenario_effects.clock = lambda: NOW.replace(hour=16, minute=30)  # 19:30 MSK
    await service.process(event("/start", 42))
    await service.process(event("continue", 43))
    due = daytime(NOW.replace(hour=17, minute=30))
    assert jobs(api)["idle:0"]["due_at"] == due.isoformat()
    service._scenario_effects.clock = lambda: due
    api.replies.append({"turn_key": "scenario-followup:idle:0"})
    assert not await service.send_due_followup(23)
    assert jobs(api)["idle:0"]["state"] == "sent"

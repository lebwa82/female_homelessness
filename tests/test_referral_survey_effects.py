from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, ordinary_evaluation

from app.chatwoot.scenario_effects import ScenarioEffects, daytime
from app.chatwoot.service import ChatwootAgentService

NOW = datetime(2026, 11, 1, 10, tzinfo=UTC)
REFERRAL = "referral:source"


@pytest.fixture
def runtime():
    api = FakeChatwoot()
    queues = SimpleNamespace(default_id=9, route=AsyncMock())
    return api, ScenarioEffects(api, queues, clock=lambda: NOW)


def attrs(api):
    return api.conversation["custom_attributes"]


def jobs(api):
    return attrs(api).get("scenario_followups", {})


def action(kind, key="a" * 24, **payload):
    return [(23, "scenario_event", "completed", {"kind": kind, "key": key, **payload})]


async def certificate(effects, key="issued"):
    await effects.certificate_delivered(23, key, {"aid_id": "food_card"})


def due_job(screen="s7"):
    return {"screen": screen, "state": "pending", "due_at": NOW.isoformat(), "context": {}}


async def test_successful_certificate_schedules_once_without_replacing_other_jobs(runtime):
    api, effects = runtime
    existing = {"review:existing": due_job()}
    attrs(api)["scenario_followups"] = deepcopy(existing)
    await certificate(effects)
    first = deepcopy(jobs(api)[REFERRAL])
    assert first == {
        "screen": "s39", "state": "pending",
        "due_at": (NOW + timedelta(minutes=3)).isoformat(), "context": {},
    }
    assert jobs(api)["review:existing"] == existing["review:existing"]
    assert jobs(api)["certificate:issued"]["screen"] == "s5"
    effects.clock = lambda: NOW + timedelta(hours=1)
    await certificate(effects)
    await certificate(effects, "second-issued")
    assert jobs(api)[REFERRAL] == first


async def test_consultation_acceptance_schedules_once_across_restart_and_clear(runtime):
    api, effects = runtime
    event = action("consultation_requested", aid_id="legal_consultation")
    await effects.apply(23, 42, event)
    original = deepcopy(jobs(api)[REFERRAL])
    assert "check:" + "a" * 24 not in jobs(api)  # Outcome surveys require staff confirmation.
    attrs(api).update(scenario={}, workflow_state="greeting", context_epoch=42)
    restarted = ScenarioEffects(api, effects.queues, clock=lambda: NOW + timedelta(hours=1))
    await restarted.apply(23, 42, event)
    await restarted.apply(23, 43, action("consultation_requested", "b" * 24,
                                      aid_id="peer_consultation"))
    assert jobs(api)[REFERRAL] == original
    assert len(effects.queues.route.await_args_list) == 2


@pytest.mark.parametrize("kind", ["certificate_waitlist", "certificate_problem",
                                 "consultation_retry", "other_request"])
async def test_unsuccessful_or_unrelated_events_do_not_schedule(runtime, kind):
    api, effects = runtime
    await effects.apply(23, 42, action(kind, aid_id="food_card"))
    assert REFERRAL not in jobs(api)


async def test_historical_requests_and_delivered_certificate_are_not_backfilled(runtime):
    api, effects = runtime
    attrs(api)["scenario_requests"] = {
        "a" * 24: {"aid_id": "legal_consultation", "state": "requested",
                   "requested_at": (NOW - timedelta(days=1)).isoformat()},
    }
    attrs(api)["scenario_followups"] = {"certificate:issued": due_job("s5")}
    await effects.apply(23, 42, action("consultation_requested", aid_id="legal_consultation"))
    await certificate(effects)
    assert REFERRAL not in jobs(api)


async def test_historical_conversation_does_not_create_job_when_polling(runtime):
    api, effects = runtime
    attrs(api).update(scenario_certificate={"aid_id": "food_card"},
                      scenario_requests={"old": {"state": "requested"}})
    assert await effects.due(api.conversation) is None
    assert api.attributes == []


async def test_referral_uses_existing_daytime_window(runtime):
    api, effects = runtime
    late = NOW.replace(hour=16, minute=59)
    effects.clock = lambda: late
    await certificate(effects)
    assert jobs(api)[REFERRAL]["due_at"] == daytime(late + timedelta(minutes=3)).isoformat()
    assert datetime.fromisoformat(jobs(api)[REFERRAL]["due_at"]).day == late.day + 1


@pytest.mark.parametrize("screen", ["s2", "s31b", "s32b", "s33c", "s37b", "s34b",
                                   "s35c", "s38b", "i2", "s1a"])
async def test_due_referral_only_uses_completed_or_menu_screens(runtime, screen):
    api, effects = runtime
    await certificate(effects)
    attrs(api)["scenario"] = {"screen": screen}
    effects.clock = lambda: NOW + timedelta(minutes=3)
    assert (await effects.due(api.conversation))[0] == REFERRAL


@pytest.mark.parametrize("changes", [
    {"scenario": {"screen": "s35b"}},
    {"scenario": {"screen": "s35d"}},
    {"scenario": {"screen": "s34", "draft_key": "draft"}},
    {"scenario": {"screen": "s7", "job_key": "review:existing"}},
    {"scenario": {"screen": "s2", "awaiting_text": True}},
    {"scenario_pending_input": {"message_id": 42}},
    {"pending_aid_id": "legal_consultation"},
    {"pending_contact_method": "phone"},
    {"pending_city": "test-city"},
    {"pending_district": "test-district"},
    {"pending_offer": "draft"},
    {"workflow_state": "collecting_contact_value"},
])
async def test_referral_waits_for_active_workflow_without_mutating_it(runtime, changes):
    api, effects = runtime
    await certificate(effects)
    attrs(api).update(scenario={"screen": "s2"}, **{
        key: value for key, value in changes.items() if key != "scenario"
    })
    if "scenario" in changes:
        attrs(api)["scenario"] = changes["scenario"]
    before = deepcopy(attrs(api))
    effects.clock = lambda: NOW + timedelta(minutes=3)
    assert await effects.due(api.conversation) is None
    assert attrs(api) == before


async def test_referral_can_recover_its_own_unsent_screen(runtime):
    api, effects = runtime
    await certificate(effects)
    attrs(api)["scenario"] = {"screen": "s39", "job_key": REFERRAL}
    effects.clock = lambda: NOW + timedelta(minutes=3)
    assert (await effects.due(api.conversation))[0] == REFERRAL


async def test_deferred_referral_does_not_block_other_due_jobs(runtime):
    api, effects = runtime
    await certificate(effects)
    jobs(api)["other"] = due_job("s5")
    attrs(api)["scenario"] = {"screen": "s35b"}
    effects.clock = lambda: NOW + timedelta(minutes=3)
    assert (await effects.due(api.conversation))[0] == "other"


@pytest.mark.parametrize("source,label", [
    ("website", "Сайт"), ("friend", "Знакомые"), ("organization", "Организация"),
    ("leaflet", "Листовка"), ("other", "Другое"), ("skipped", "Пропущено"),
])
async def test_referral_answer_is_durable_readable_and_replay_safe(runtime, source, label):
    api, effects = runtime
    await certificate(effects)
    jobs(api)["review:existing"] = due_job()
    payload = {"source": source, "job_key": REFERRAL}
    if source == "other":
        payload["text"] = "test-source"
    event = action("referral_source", **payload)
    await effects.apply(23, 42, event)
    result = attrs(api)["scenario_referral_source"]
    assert result == {"source": source, "answered_at": NOW.isoformat(),
                      **({"text": "test-source"} if source == "other" else {})}
    assert label in api.notes[0]
    assert jobs(api)[REFERRAL]["state"] == "answered"
    assert jobs(api)["review:existing"]["state"] == "pending"
    attrs(api).update(scenario={}, context_epoch=42)
    effects.clock = lambda: NOW + timedelta(hours=1)
    await effects.apply(23, 42, event)
    await certificate(effects, "second-issued")
    assert attrs(api)["scenario_referral_source"] == result
    assert len(api.notes) == 1
    assert jobs(api)[REFERRAL]["state"] == "answered"


async def test_lost_note_ack_preserves_first_answer_timestamp(runtime, monkeypatch):
    api, effects = runtime
    event = action("referral_source", source="website", job_key=REFERRAL)
    original = api.add_private_note

    async def fail_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("lost_note_ack")

    monkeypatch.setattr(api, "add_private_note", fail_after_write)
    with pytest.raises(RuntimeError, match="lost_note_ack"):
        await effects.apply(23, 42, event)
    first = deepcopy(attrs(api)["scenario_referral_source"])
    monkeypatch.setattr(api, "add_private_note", original)
    effects.clock = lambda: NOW + timedelta(hours=1)
    await effects.apply(23, 42, event)
    assert attrs(api)["scenario_referral_source"] == first
    assert len(api.notes) == 1


async def test_sent_referral_has_no_reminder_and_never_rearms(runtime):
    api, effects = runtime
    await certificate(effects)
    await effects.sent(23, REFERRAL)
    assert jobs(api)[REFERRAL]["state"] == "sent"
    assert not any(key.startswith("reminder:") for key in jobs(api))
    await effects.apply(23, 42, action("consultation_requested", aid_id="legal_consultation"))
    assert jobs(api)[REFERRAL]["state"] == "sent"


@pytest.mark.parametrize("blocker", ["human", "snoozed", "pending_input", "active_form"])
async def test_service_referral_respects_ownership_and_active_input(runtime, blocker):
    api, effects = runtime
    await certificate(effects)
    attrs(api)["scenario"] = {"screen": "s2"}
    if blocker == "human":
        await api.assign_human(23, 4)
    elif blocker == "snoozed":
        api.conversation["status"] = "snoozed"
    elif blocker == "pending_input":
        attrs(api)["scenario_pending_input"] = {"message_id": 42}
    else:
        attrs(api)["scenario"] = {"screen": "s35d", "awaiting_text": True}
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9)
    service._scenario_effects.clock = lambda: NOW + timedelta(minutes=3)
    before = deepcopy(attrs(api)["scenario"])
    assert not await service.send_due_followup(23)
    assert attrs(api)["scenario"] == before
    assert jobs(api)[REFERRAL]["state"] == "pending"
    assert api.replies == []


async def test_service_lost_referral_ack_recovers_without_duplicate_or_reminder(runtime, monkeypatch):
    api, effects = runtime
    await certificate(effects)
    attrs(api)["scenario"] = {"screen": "s34b"}
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9)
    service._scenario_effects.clock = lambda: NOW + timedelta(minutes=2)
    assert not await service.send_due_followup(23)
    service._scenario_effects.clock = lambda: NOW + timedelta(minutes=3)
    original = api.send_reply

    async def fail_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("lost_reply_ack")

    monkeypatch.setattr(api, "send_reply", fail_after_write)
    with pytest.raises(RuntimeError, match="lost_reply_ack"):
        await service.send_due_followup(23)
    assert attrs(api)["scenario"]["screen"] == "s39"
    monkeypatch.setattr(api, "send_reply", original)
    assert not await service.send_due_followup(23)
    assert len(api.replies) == 1
    assert jobs(api)[REFERRAL]["state"] == "sent"
    assert not any(key.startswith("reminder:") for key in jobs(api))

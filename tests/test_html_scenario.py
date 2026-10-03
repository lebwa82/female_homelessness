from datetime import UTC, datetime, timedelta

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation

from app.chatwoot.contracts import CONSULTATION_COMPLETED, StaffMessage, parse_staff_message
from app.chatwoot.scenario_effects import daytime, returning_after_day
from app.chatwoot.service import ChatwootAgentService
from app.config import settings
from app.domain import IncomingMessage
from app.scenario import (
    AIDS,
    CERTIFICATES,
    CONSULTATION_IDS,
    SCREENS,
    button_indices,
    certificate_context,
    render,
)
from app.service import ConversationService
from app.store import InMemoryConversationStore, StoredCertificate
from scripts.import_scenario import SOURCE, TARGET, compile_copy


def test_html_copy_is_generated_and_has_valid_targets():
    assert TARGET.read_text() == compile_copy(SOURCE.read_text())
    assert len(SCREENS) == 57
    assert all(b["target"] in SCREENS for s in SCREENS.values() for b in s["buttons"])


@pytest.mark.parametrize("screen", tuple(SCREENS))
def test_each_screen_variant_renders_without_duplicate_buttons(screen):
    if screen == "i3":  # Navigation reference, not a message screen.
        return
    count = len(SCREENS[screen]["texts"]) - int(screen == "s51")
    for variant in range(count):
        turn = render(screen, variant=variant)
        assert 0 < len(turn.text) < 4096
        assert len({c.id for c in turn.choices}) == len(turn.choices)
        assert sum(c.id == "human" for c in turn.choices) == 1
        assert all(len(c.id.encode()) <= 64 for c in turn.choices)


@pytest.fixture
async def flow(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=StubGateway(ordinary_evaluation()),
                                  html_scenario=True)
    incoming = IncomingMessage(channel="chatwoot", platform_user_id=7, chat_id=23,
                               text="/start", message_id=1)
    await service.start(incoming)
    record = await store.get(incoming)
    store.actions.clear()
    return service, record, store


def choice_for(turn, target):
    screen = turn.audit["scenario_screen"]
    for choice in turn.choices:
        if choice.id.startswith("sc:"):
            index = int(choice.id.split(":")[2])
            if SCREENS[screen]["buttons"][index]["target"] == target:
                return choice.id
    raise AssertionError(f"missing transition to {target}")


@pytest.mark.asyncio
async def test_start_menu_and_catalog_are_explicit(flow):
    service, record, store = flow
    menu = await service.scenario_flow.callback(record, "continue", "m2")
    assert menu.audit["scenario_screen"] == "s2"
    assert len([c for c in menu.choices if c.id.startswith("need:")]) == 6
    for need, target, aids in (
        ("legal", "s23", {"legal_consultation"}),
        ("children", "s25", {"children_card", "legal_consultation", "psychologist_3_sessions"}),
    ):
        await service.scenario_flow.show(record, "s2")
        result = await service.scenario_flow.callback(record, f"need:{need}", "m3")
        assert result.audit["scenario_screen"] == target
        assert {c.id[4:] for c in result.choices if c.id.startswith("aid:")} == aids
    assert not store.aid_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("screen", ("s34", "s35", "s38"))
async def test_consultation_requires_confirmation_and_same_draft_is_idempotent(flow, screen):
    service, record, store = flow
    scene = service.scenario_flow
    preview = await scene.show(record, screen, draft_key="draft")
    assert not store.actions
    confirm = next(c.id for c in preview.choices if c.id.startswith("sc:"))
    await scene.callback(record, confirm, "first-click")
    assert len([a for a in store.actions if a[1] == "scenario_event"]) == 1
    assert record.scenario["aid_id"] == AIDS[screen]
    previous = await scene.show(record, screen, draft_key="draft")
    await scene.callback(record, next(c.id for c in previous.choices if c.id.startswith("sc:")),
                         "different-click")
    assert len([a for a in store.actions if a[1] == "scenario_event"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("screen,target", (("s35d", "s35c"), ("s36", "s36b"),
                                         ("s6", "s61"), ("s74", "s75")))
async def test_write_button_does_not_submit_an_empty_request(flow, screen, target):
    service, record, store = flow
    scene = service.scenario_flow
    turn = await scene.show(record, screen)
    await scene.callback(record, choice_for(turn, target), "write")
    assert not store.actions
    assert record.scenario["awaiting_text"]
    await scene.text(record, "Synthetic feedback", "text")
    assert record.scenario["screen"] == target
    assert len(store.actions) == 1


@pytest.mark.asyncio
async def test_waitlist_is_opt_in_and_ack_has_no_repeat_submission(flow):
    service, record, store = flow
    turn = await service.scenario_flow.show(record, "i8", aid_id="food_card")
    assert not store.actions
    reply = await service.scenario_flow.callback(record, choice_for(turn, "i8"), "submit")
    assert store.actions[0][3]["kind"] == "certificate_waitlist"
    assert not any(c.id.startswith("sc:") for c in reply.choices)


@pytest.mark.asyncio
async def test_stale_callback_cannot_create_request(flow):
    service, record, store = flow
    preview = await service.scenario_flow.show(record, "s34")
    callback = choice_for(preview, "s34b")
    await service.scenario_flow.show(record, "s2")
    await service.scenario_flow.callback(record, callback, "old-click")
    assert record.scenario["screen"] == "s2"
    assert not store.actions


def test_test_certificate_uses_seven_days_and_navigation_does_not_reveal_code():
    issued = datetime(2026, 10, 3, 10, tzinfo=UTC)
    certificate = StoredCertificate(aid_id="food_card", provider="test", nominal_rubles=3000,
                                    activation_code="TEST-ONLY", serial_number="TEST",
                                    issued_at=issued, expires_at=issued + timedelta(days=90), is_test=True)
    context = certificate_context(certificate)
    assert datetime.fromisoformat(context["expires_at"]) == issued + timedelta(days=7)
    assert "TEST-ONLY" in render("s31b", context=context).text
    assert "TEST-ONLY" not in render("s31b", context={"aid_id": "food_card"}).text


@pytest.mark.parametrize("hour,day,expected", ((5, 3, 7), (9, 3, 9), (17, 4, 7), (22, 4, 7)))
def test_daytime_schedule(hour, day, expected):
    due = daytime(datetime(2026, 10, 3, hour, tzinfo=UTC))
    assert (due.day, due.hour) == (day, expected)


def test_only_private_authenticated_staff_can_complete_consultation():
    payload = {"event": "message_created", "message_type": "outgoing", "private": True,
               "id": 12, "conversation": {"id": 23}, "sender": {"type": "user", "id": 4},
               "content": CONSULTATION_COMPLETED}
    assert parse_staff_message(payload).consultation_completed
    assert not parse_staff_message({**payload, "private": False}).consultation_completed
    assert parse_staff_message({**payload, "sender": {"type": "contact", "id": 4}}) is None
    assert parse_staff_message({**payload, "sender": {"type": "agent_bot", "id": 4}}) is None


@pytest.mark.asyncio
async def test_completion_closes_chat_and_next_input_reopens_bot(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(reply_owner="human", ownership_version=2,
        scenario_requests={"a" * 24: {"aid_id": "legal_consultation", "state": "requested"}})
    api.conversation["assignee_id"] = 4
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    await service.process(StaffMessage(71, 23, 4, consultation_completed=True))
    assert api.conversation["status"] == "resolved"
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"
    jobs = api.conversation["custom_attributes"]["scenario_followups"]
    assert len(jobs) == 1
    assert next(iter(jobs.values()))["screen"] == "s7"
    assert await service.process(event("Hello", 72))
    assert api.conversation["status"] == "pending"
    assert len(api.replies) == 1


@pytest.mark.asyncio
async def test_multiple_consultations_require_explicit_completion_id():
    api = FakeChatwoot()
    api.conversation["custom_attributes"]["scenario_requests"] = {
        k * 24: {"aid_id": aid, "state": "requested"}
        for k, aid in zip(("a", "b"), CONSULTATION_IDS, strict=False)
    }
    service = ChatwootAgentService(api)
    await service.process(StaffMessage(71, 23, 4, consultation_completed=True))
    assert not api.statuses
    assert "completion-ambiguous:71" in api.note_keys
    await service.process(StaffMessage(72, 23, 4, consultation_completed=True, request_id="a" * 24))
    assert api.conversation["custom_attributes"]["scenario_requests"]["b" * 24]["state"] == "requested"


@pytest.mark.asyncio
@pytest.mark.parametrize("aid_id", CERTIFICATES + CONSULTATION_IDS)
async def test_survey_timers_survive_restart_and_any_answer_cancels_reminder(aid_id):
    api = FakeChatwoot()
    now = datetime(2026, 10, 3, 10, tzinfo=UTC)
    api.conversation["custom_attributes"]["scenario_followups"] = {
        "first": {"state": "pending", "screen": "s5", "due_at": now.isoformat(),
                  "context": {"aid_id": aid_id, "expires_at": (now + timedelta(days=3)).isoformat()}}
    }
    service = ChatwootAgentService(api)
    service._scenario_effects.clock = lambda: now
    assert await service.send_due_followup(23)
    assert len(api.replies) == 1
    jobs = api.conversation["custom_attributes"]["scenario_followups"]
    assert jobs["reminder:first"]["state"] == "pending"
    restarted = ChatwootAgentService(api)
    restarted._scenario_effects.clock = lambda: now
    assert not await restarted.send_due_followup(23)
    await restarted._scenario_effects.answer_current(api.conversation)
    assert api.conversation["custom_attributes"]["scenario_followups"]["reminder:first"]["state"] == "answered"


def test_variant_buttons_preserve_distinct_answers():
    for screen in ("s71", "s72", "s73"):
        turn = render(screen)
        choices = [c for c in turn.choices if c.id != "human"]
        assert len(choices) == 3
        assert len({c.id for c in choices}) == 3
    assert len(button_indices("s5", 7)) == 4


@pytest.mark.asyncio
async def test_partial_delivery_retry_does_not_lose_or_duplicate_consultation():
    class FailureAfterWrite(FakeChatwoot):
        fail = False

        async def send_reply(self, *args, **kwargs):
            if self.fail:
                self.fail = False
                assert self.conversation["custom_attributes"]["scenario_requests"]
                assert self.notes
                raise ConnectionError("synthetic")
            return await super().send_reply(*args, **kwargs)

    api = FailureAfterWrite()
    service = ChatwootAgentService(api, duty_team_id=9)
    await service.process(event("/start", 41))
    await service.process(event("continue", 42))
    await service.process(event("need:legal", 43))
    await service.process(event("aid:legal_consultation", 44))
    callback = next(c.id for c in api.replies[-1]["choices"] if c.id.startswith("sc:"))
    api.fail = True
    with pytest.raises(ConnectionError):
        await service.process(event(callback, 45))
    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "s35b"
    # A new service object models a process restart between effect and reply.
    restarted = ChatwootAgentService(api, duty_team_id=9)
    assert await restarted.process(event(callback, 45))
    assert len(api.conversation["custom_attributes"]["scenario_requests"]) == 1
    assert len(api.conversation["custom_attributes"]["scenario_followups"]) == 2  # cancelled idle + check
    assert len([n for n in api.note_keys if n.startswith("scenario:")]) == 1
    await restarted.process(event("/clear", 46))
    assert len(api.conversation["custom_attributes"]["scenario_requests"]) == 1
    assert len(api.conversation["custom_attributes"]["scenario_followups"]) == 2
    assert not await restarted.process(event(callback, 45))


@pytest.mark.asyncio
async def test_no_survey_while_human_no_expired_reminder():
    api = FakeChatwoot()
    service = ChatwootAgentService(api, duty_team_id=9)
    now = datetime(2026, 10, 3, 10, tzinfo=UTC)
    service._scenario_effects.clock = lambda: now
    await service.process(event("/start", 41))
    await service.process(event("continue", 42))
    assert api.conversation["custom_attributes"]["scenario_followups"]["idle:0"]["state"] == "pending"
    await service._scenario_effects.certificate_delivered(23, "cert", {
        "aid_id": "food_card", "expires_at": (now + timedelta(days=7)).isoformat(),
    })
    service._scenario_effects.clock = lambda: now + timedelta(days=4)
    api.conversation["custom_attributes"].update(reply_owner="human", ownership_version=2)
    api.conversation["assignee_id"] = 4
    assert not await service.send_due_followup(23)
    api.conversation["assignee_id"] = None
    api.conversation["custom_attributes"]["reply_owner"] = "bot"
    service._scenario_effects.clock = lambda: now + timedelta(days=8)
    assert not await service.send_due_followup(23)
    assert api.conversation["custom_attributes"]["scenario_followups"]["certificate:cert"]["state"] == "expired"


@pytest.mark.asyncio
async def test_certificate_prioritizes_block_five_and_completion_review_has_no_reminder():
    api = FakeChatwoot()
    service = ChatwootAgentService(api, duty_team_id=9)
    now = datetime(2026, 10, 3, 10, tzinfo=UTC)
    effects = service._scenario_effects
    effects.clock = lambda: now
    await effects.apply(23, 41, [(23, "scenario_event", "completed", {
        "key": "a" * 24, "kind": "consultation_requested", "aid_id": "legal_consultation",
    })])
    await effects.certificate_delivered(23, "cert", {
        "aid_id": "food_card", "expires_at": (now + timedelta(days=7)).isoformat(),
    })
    jobs = api.conversation["custom_attributes"]["scenario_followups"]
    assert jobs["check:" + "a" * 24]["state"] == "cancelled"
    await service.process(StaffMessage(43, 23, 4, consultation_completed=True))
    effects.clock = lambda: now + timedelta(hours=2)
    assert await service.send_due_followup(23)
    jobs = api.conversation["custom_attributes"]["scenario_followups"]
    assert jobs["review:" + "a" * 24]["state"] == "sent"
    assert not any(k.startswith("reminder:review:") for k in jobs)


@pytest.mark.asyncio
async def test_survey_reply_lost_response_is_reconciled_without_second_message():
    api = FakeChatwoot()
    service = ChatwootAgentService(api)
    now = datetime(2026, 10, 3, 10, tzinfo=UTC)
    service._scenario_effects.clock = lambda: now
    api.conversation["custom_attributes"]["scenario_followups"] = {
        "job": {"screen": "s5", "state": "pending", "due_at": now.isoformat(),
                "context": {"aid_id": "food_card"}}
    }
    api.replies.append({"turn_key": "scenario-followup:job"})
    assert not await service.send_due_followup(23)
    assert len(api.replies) == 1
    assert api.conversation["custom_attributes"]["scenario_followups"]["job"]["state"] == "sent"


@pytest.mark.asyncio
async def test_returning_after_day_keeps_actual_answer(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    api = FakeChatwoot()
    now = datetime(2026, 10, 3, 10, tzinfo=UTC)
    api.messages = ({"id": 40, "message_type": 0, "content": "Hello", "private": False,
                     "created_at": (now - timedelta(days=1)).timestamp()},)
    assert returning_after_day(api.messages, 41, now)
    assert not returning_after_day(api.messages, 41, now - timedelta(seconds=1))
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    service._scenario_effects.clock = lambda: now
    assert await service.process(event("Hello", 41))
    assert ordinary_evaluation().support.draft_text in api.replies[-1]["text"]
    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "i9"


@pytest.mark.asyncio
async def test_critical_signal_preempts_optional_form(flow):
    from app.agents import AgentEvaluation
    from app.domain import DiagnosticStatus, RiskLevel, SafetyDiagnostic, SafetyEscalation

    service, record, store = flow
    await service.scenario_flow.show(record, "s74")
    service.gateway = StubGateway(AgentEvaluation(
        safety=SafetyDiagnostic(level=RiskLevel.URGENT, escalation=SafetyEscalation.HANDOFF),
        support=ordinary_evaluation().support,
        safety_status=DiagnosticStatus.COMPLETED, support_status=DiagnosticStatus.COMPLETED,
    ))
    # Neutral input plus a controlled diagnostic object, not sensitive raw fixtures.
    await service.handle_text(IncomingMessage(channel="chatwoot", platform_user_id=7,
                                             chat_id=23, text="Synthetic input", message_id=2))
    assert not any(a[1] == "scenario_event" for a in store.actions)
    assert any(a[1] == "safety_escalation" for a in store.actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("screen", tuple(SCREENS))
async def test_every_explicit_html_callback_has_a_runtime_transition(flow, screen):
    if screen == "i3":
        return
    service, record, _ = flow
    scene = service.scenario_flow
    count = len(SCREENS[screen]["texts"]) - int(screen == "s51")
    for variant in range(count):
        context = {"aid_id": "food_card", "request_key": "test-request"}
        if screen in {"s7", "s5"}:
            if screen == "s7":
                context["aid_id"] = CONSULTATION_IDS[variant]
            elif variant < 4:
                context["aid_id"] = CERTIFICATES[variant]
            else:
                context["aid_id"] = CONSULTATION_IDS[min(variant - 4, 2)]
                context["completed"] = variant == 7
        if screen == "s51":
            context["aid_id"] = CERTIFICATES[variant]
        if screen in {"s56", "s59"} and variant == 1:
            context["aid_id"] = CONSULTATION_IDS[0]
        for index in button_indices(screen, variant):
            await scene.show(record, screen, variant=variant, **context)
            callback = f"sc:{screen}:{index}:{record.navigation.get('revision', 0)}"
            if callback not in {c.id for c in scene.current(record).choices}:
                continue  # Legacy entry/certificate callbacks have dedicated tests.
            result = await scene.callback(record, callback, f"test:{screen}:{variant}:{index}")
            assert result is not None
            assert record.scenario["screen"] in SCREENS


@pytest.mark.asyncio
async def test_followup_sweep_handles_all_pages_and_isolates_one_failure():
    from app.chatwoot.followups import sweep

    class Api:
        async def list_conversations(self, page):
            return tuple({"id": i, "custom_attributes": {"scenario_followups": {"job": {}}}}
                         for i in {1: [1, 2], 2: [3], 3: []}[page])

    class Service:
        def __init__(self):
            self.seen = []

        async def send_due_followup(self, conversation_id):
            self.seen.append(conversation_id)
            if conversation_id == 1:
                raise ConnectionError("synthetic")
            return True

    service = Service()
    assert await sweep(Api(), service) == 2
    assert service.seen == [1, 2, 3]


@pytest.mark.asyncio
async def test_lost_reply_acknowledgement_does_not_leave_followups_blocked():
    api = FakeChatwoot()
    api.conversation["custom_attributes"]["scenario_pending_input"] = {"message_id": 41}
    api.replies.append({"turn_key": "message:41"})
    assert not await ChatwootAgentService(api).process(event("/start", 41))
    assert api.conversation["custom_attributes"]["scenario_pending_input"] is None


def test_legacy_checkpoint_cannot_keep_newer_scenario():
    from app.navigation import back_update

    navigation = {"revision": 2, "cursor": 1, "entries": [
        {"previous": None, "workflow": {"state": "choosing_aid", "need": "legal"}},
        {"previous": 0, "workflow": {"state": "choosing_aid", "need": "food_money",
                                       "scenario": {"screen": "s22"}}},
    ]}
    assert back_update(navigation, "back:2")["scenario"] == {}

"""Menu navigation keeps durable effects and accepts previously delivered controls."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation

from app.chatwoot.service import ChatwootAgentService
from app.domain import IncomingMessage
from app.scenario import SCREENS, render
from app.service import ConversationService
from app.store import InMemoryConversationStore, StoredCertificate


def incoming(message_id):
    return IncomingMessage(channel="chatwoot", platform_user_id=7, chat_id=23,
                           text="", message_id=message_id)


def home(turn):
    choices = [choice for choice in turn.choices if choice.label == "Вернуться в начало"]
    assert len(choices) == 1
    assert choices[0].id.startswith("home:")
    assert all(choice.label != "Вернуться на шаг назад" for choice in turn.choices)
    return choices[0].id


@pytest.fixture
async def flow():
    store = InMemoryConversationStore()
    service = ConversationService(store=store, html_scenario=True)
    await service.start(incoming(1))
    record = await store.get(incoming(1))
    return service, record, store


@pytest.mark.parametrize("screen", tuple(SCREENS))
async def test_all_screen_variants_have_one_home_except_greeting_and_menu(flow, screen):
    if screen == "i3":  # This is a navigation reference, not a message screen.
        return
    service, record, _ = flow
    for variant in range(len(SCREENS[screen]["texts"]) - int(screen == "s51")):
        turn = service._with_back_choice(record, render(screen, variant=variant))
        assert not any(choice.id.startswith("back:") for choice in turn.choices)
        if turn.audit["scenario_screen"] in {"s1", "s2"}:
            assert not any(choice.id.startswith("home:") for choice in turn.choices)
        else:
            home(turn)


@pytest.mark.parametrize("screen", ("s21", "s23", "s34", "s35", "s38", "s35d", "s6",
                                    "i8", "s31b", "s5", "i1"))
async def test_home_returns_to_second_step_without_business_effects(flow, screen):
    service, record, store = flow
    await service.scenario_flow.show(record, screen, aid_id="food_card",
                                    draft_key="draft", job_key="existing-job")
    await store.update(record, pending_contact_method="email", pending_offer="psychologist")
    turn = service._with_back_choice(record, await service._state_turn(record))
    retained = deepcopy((store.actions, store.aid_requests, store.followup_jobs, store.messages))

    result = await service.handle_callback(incoming(2), home(turn))

    assert result.audit["scenario_screen"] == "s2"
    assert record.state == "discovering_need"
    assert record.need is None and record.pending_aid_id is None
    assert record.pending_contact_method is None and record.pending_offer is None
    assert record.scenario == {"screen": "s2", "variant": 0, "awaiting_text": False}
    assert len([choice for choice in result.choices if choice.id.startswith("need:")]) == 6
    assert not any(choice.id.startswith(("home:", "back:")) for choice in result.choices)
    assert (store.actions, store.aid_requests, store.followup_jobs) == retained[:3]
    assert store.messages[:len(retained[3])] == retained[3]


async def test_home_is_revision_bound_and_duplicate_keeps_new_branch(flow):
    service, record, store = flow
    turn = await service.scenario_flow.show(record, "s35", draft_key="draft")
    callback = home(service._with_back_choice(record, turn))
    result = await service.handle_callback(incoming(2), callback)
    assert result.audit["scenario_screen"] == "s2"
    await service.handle_callback(incoming(3), "need:children")
    assert record.scenario["screen"] == "s25"

    await service.handle_callback(incoming(2), callback)
    await service.handle_callback(incoming(4), callback)

    assert record.scenario["screen"] == "s25"
    assert not store.aid_requests


async def test_home_preserves_an_issued_certificate(flow):
    service, record, store = flow
    issued = datetime(2026, 10, 3, 10, tzinfo=UTC)
    certificate = StoredCertificate(
        aid_id="food_card", provider="test", nominal_rubles=3000,
        activation_code="TEST-ONLY", serial_number="TEST",
        issued_at=issued, expires_at=issued + timedelta(days=90),
        issued_to=record.platform_user_id, is_test=True,
    )
    store.certificates.append(certificate)
    turn = await service.scenario_flow.show(record, "s31b", aid_id="food_card")
    await service.handle_callback(incoming(2), home(service._with_back_choice(record, turn)))
    assert store.certificates == [certificate]
    assert store.certificates[0].issued_to == record.platform_user_id


@pytest.mark.parametrize("change", ("clear", "background"))
async def test_old_home_cannot_cross_clear_or_background_transition(flow, change):
    service, record, _ = flow
    turn = await service.scenario_flow.show(record, "s35")
    callback = home(service._with_back_choice(record, turn))
    if change == "clear":
        await service.clear(incoming(2))
        expected = "greeting"
    else:
        record.state, record.scenario = "open_conversation", {}
        expected = "open_conversation"
    await service.handle_callback(incoming(3), callback)
    assert record.state == expected
    assert record.scenario.get("screen") != "s2"


@pytest.mark.parametrize("screen", ("s34", "s35", "s38", "s35c", "s31b", "s75"))
async def test_legacy_scenario_navigation_skips_survey_and_request_effects(flow, screen):
    service, record, store = flow
    await service.scenario_flow.show(record, screen, aid_id="food_card", job_key="job")
    index = next(i for i, button in enumerate(SCREENS[screen]["buttons"])
                 if button["label"].startswith("Вернуться"))
    callback = f"sc:{screen}:{index}:{record.navigation['revision']}"
    retained = deepcopy(store.actions)

    result = await service.handle_callback(incoming(2), callback)

    assert result.audit["scenario_screen"] == "s2"
    assert store.actions == retained
    await service.handle_callback(incoming(3), "need:legal")
    await service.handle_callback(incoming(4), callback)
    assert record.scenario["screen"] == "s23"


async def test_old_back_still_works_without_a_visible_back_button(flow):
    service, record, _ = flow
    await service.handle_callback(incoming(2), "continue")
    turn = await service.handle_callback(incoming(3), "need:legal")
    home(turn)
    result = await service.handle_callback(incoming(4), f"back:{record.navigation['revision']}")
    assert result.audit["scenario_screen"] == "s2"


async def test_old_restart_goes_to_menu_but_start_keeps_greeting(flow):
    service, record, _ = flow
    await service.scenario_flow.show(record, "s35c")
    result = await service.handle_callback(incoming(2), "restart")
    assert result.audit["scenario_screen"] == "s2"
    greeting = await service.start(incoming(3))
    assert greeting.audit["scenario_screen"] == "s1"
    assert not any(choice.id.startswith(("home:", "back:")) for choice in greeting.choices)


async def test_chatwoot_home_preserves_requests_history_and_deduplicates():
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)
    for message_id, content in enumerate(("/start", "continue", "need:legal"), 41):
        assert await service.process(event(content, message_id))
    callback = next(choice.id for choice in api.replies[-1]["choices"]
                    if choice.label == "Вернуться в начало")
    api.conversation["custom_attributes"]["scenario_requests"] = {
        "keep": {"state": "requested", "aid_id": "legal_consultation"},
    }
    retained = deepcopy(api.conversation["custom_attributes"]["scenario_requests"])
    messages = deepcopy(api.messages)

    assert await service.process(event(callback, 44))
    assert not await service.process(event(callback, 44))

    attributes = api.conversation["custom_attributes"]
    assert attributes["scenario"]["screen"] == "s2"
    assert attributes["scenario_requests"] == retained
    assert api.messages == messages and gateway.calls == 0
    assert len(api.replies) == 4


async def test_chatwoot_home_does_not_take_conversation_from_human():
    api = FakeChatwoot()
    api.conversation.update(status="open", assignee_id=4)
    api.conversation["custom_attributes"].update(
        scenario={"screen": "s35"}, scenario_requests={"keep": {"state": "requested"}},
    )
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)

    assert await service.process(event("home:0"))
    assert not await service.process(event("home:0"))

    attributes = api.conversation["custom_attributes"]
    assert api.conversation["assignee_id"] == 4 and attributes["reply_owner"] == "human"
    assert attributes["scenario"]["screen"] == "s35"
    assert attributes["scenario_requests"]["keep"]["state"] == "requested"
    assert gateway.calls == 0 and len(api.replies) == 1

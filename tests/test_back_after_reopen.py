"""Resuming free conversation does not navigate back to a terminal screen."""

from copy import deepcopy

import pytest

from app.domain import IncomingMessage
from app.service import ConversationService
from app.store import ConversationRecord, InMemoryConversationStore
from tests.test_chatwoot_service import StubGateway, ordinary_evaluation


def incoming(message_id, text=""):
    return IncomingMessage(
        channel="chatwoot",
        platform_user_id=901,
        chat_id=901,
        message_id=message_id,
        text=text,
    )


def service_with_root(screen="i2", state="scenario"):
    record = ConversationRecord(
        id=1,
        channel="chatwoot",
        platform_user_id=901,
        chat_id=901,
        username=None,
        state=state,
        scenario={"screen": screen, "variant": 0},
    )
    store = InMemoryConversationStore(conversations={901: record})
    service = ConversationService(
        store, gateway=StubGateway(ordinary_evaluation()), html_scenario=True
    )
    return service, store, record


async def test_reopened_i2_only_root_has_no_back_choice():
    service, _, record = service_with_root()

    resumed = await service.handle_text(incoming(1, "Hello"))

    assert record.state == "open_conversation" and not record.scenario
    assert not any(choice.id.startswith("back:") for choice in resumed.choices)


@pytest.mark.parametrize("revision_offset", [0, -1])
async def test_forged_or_stale_back_cannot_restore_i2_only_root(revision_offset):
    service, store, record = service_with_root()
    terminal = await service._state_turn(record)
    await store.record_action(record, "synthetic_business_event", "completed", {"key": "keep"})
    await service.handle_text(incoming(1, "Hello"))
    journal = deepcopy(record.navigation)
    messages = deepcopy(store.messages)
    actions = deepcopy(store.actions)
    token = f"back:{record.navigation['revision'] + revision_offset}"

    restored = await service.handle_callback(incoming(2), token)

    assert record.state == "open_conversation" and not record.scenario
    assert restored.text != terminal.text
    assert record.navigation == journal
    assert store.messages[: len(messages)] == messages
    assert store.actions[: len(actions)] == actions


async def test_reopened_conversation_back_skips_i2_to_meaningful_screen():
    service, store, record = service_with_root("s2", "discovering_need")
    await service.scenario_flow.show(record, "i2", variant=1)
    await store.record_action(record, "synthetic_business_event", "completed", {"key": "keep"})
    resumed = await service.handle_text(incoming(1, "Hello"))
    journal = deepcopy(record.navigation)
    messages = deepcopy(store.messages)
    actions = deepcopy(store.actions)
    assert not any(choice.id.startswith("back:") for choice in resumed.choices)
    token = f"back:{record.navigation['revision']}"  # Previously delivered legacy button.

    await service.handle_callback(incoming(2), token)

    assert record.state == "discovering_need" and record.scenario["screen"] == "s2"
    assert record.navigation["entries"] == journal["entries"]
    assert store.messages[: len(messages)] == messages
    assert store.actions[: len(actions)] == actions


async def test_ordinary_scenario_legacy_back_is_unchanged():
    service, _, record = service_with_root("s2", "discovering_need")
    turn = await service.handle_callback(incoming(1), "need:legal")
    assert not any(choice.id.startswith("back:") for choice in turn.choices)
    token = f"back:{record.navigation['revision']}"

    await service.handle_callback(incoming(2), token)

    assert record.state == "discovering_need" and record.scenario["screen"] == "s2"


async def test_clear_still_disconnects_navigation_without_deleting_retained_data():
    service, store, record = service_with_root("s2", "discovering_need")
    await service.scenario_flow.show(record, "i2", variant=1)
    resumed = await service.handle_text(incoming(1, "Hello"))
    assert not any(choice.id.startswith("back:") for choice in resumed.choices)
    token = f"back:{record.navigation['revision']}"
    await store.record_action(record, "synthetic_business_event", "completed", {"key": "keep"})
    entries = deepcopy(record.navigation["entries"])
    messages = deepcopy(store.messages)
    actions = deepcopy(store.actions)

    welcome = await service.clear(incoming(2))
    assert not any(choice.id.startswith("back:") for choice in welcome.choices)
    await service.handle_callback(incoming(3), token)

    assert record.state == "greeting" and record.scenario["screen"] == "s1"
    assert record.navigation["entries"][: len(entries)] == entries
    assert store.messages[: len(messages)] == messages
    assert store.actions[: len(actions)] == actions

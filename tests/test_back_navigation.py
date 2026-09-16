import copy

import pytest

from app.domain import AgentTurn, InboundExecutionKey, IncomingMessage
from app.service import ConversationService
from app.store import InMemoryConversationStore


def incoming(message_id: int) -> IncomingMessage:
    return IncomingMessage(platform_user_id=901, chat_id=901, message_id=message_id, text="")


def back(turn: AgentTurn) -> str:
    choices = [choice for choice in turn.choices if choice.label == "Вернуться на шаг назад"]
    assert len(choices) == 1
    assert any(choice.id == "human" for choice in turn.choices)
    return choices[0].id


async def begin(service: ConversationService) -> AgentTurn:
    await service.start(incoming(1))
    await service.handle_callback(incoming(2), "continue")
    return await service.handle_callback(incoming(3), "need:legal")


@pytest.mark.asyncio
async def test_back_restores_previous_states_and_keeps_message_history() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store)
    await begin(service)
    contact = await service.handle_callback(incoming(4), "aid:legal_consultation")
    history = copy.deepcopy(store.messages)

    # Recreating the service must not lose navigation.
    service = ConversationService(store)
    aid = await service.handle_callback(incoming(5), back(contact))
    record = store.conversations[901]
    assert record.state == "choosing_aid"
    assert record.pending_aid_id is None
    assert any(choice.id == "aid:legal_consultation" for choice in aid.choices)
    assert store.messages[: len(history)] == history

    needs = await service.handle_callback(incoming(6), back(aid))
    assert record.state == "discovering_need"
    assert record.need is None
    welcome = await service.handle_callback(incoming(7), back(needs))
    assert record.state == "greeting"
    assert {choice.id for choice in welcome.choices} == {"continue", "pause", "human"}


@pytest.mark.asyncio
async def test_duplicate_or_stale_back_cannot_skip_another_state() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store)
    await begin(service)
    contact = await service.handle_callback(incoming(4), "aid:legal_consultation")
    callback = back(contact)
    first = await service.handle_callback(incoming(5), callback)
    await service.handle_callback(incoming(5), callback)
    await service.handle_callback(incoming(6), callback)
    assert store.conversations[901].state == "choosing_aid"
    needs = await service.handle_callback(incoming(7), back(first))
    assert store.conversations[901].state == "discovering_need"
    assert any(choice.id == "need:legal" for choice in needs.choices)


@pytest.mark.asyncio
async def test_back_after_submission_preserves_request_followup_and_audit() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store)
    await begin(service)
    await service.handle_callback(incoming(4), "aid:legal_consultation")
    done = await service.handle_callback(incoming(5), "contact:later")
    retained = copy.deepcopy(
        (store.aid_requests, store.followup_jobs, store.actions, store.escalations)
    )

    contact = await service.handle_callback(incoming(6), back(done))

    assert store.conversations[901].state == "collecting_contact_method"
    assert store.conversations[901].pending_aid_id == "legal_consultation"
    assert any(choice.id == "contact:email" for choice in contact.choices)
    assert (store.aid_requests, store.followup_jobs, store.actions, store.escalations) == retained


@pytest.mark.asyncio
async def test_back_tracks_new_branch_and_does_not_cross_clear_boundary() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store)
    food = await begin(service)
    await service.handle_callback(incoming(4), back(food))
    legal = await service.handle_callback(incoming(5), "need:legal")
    await service.handle_callback(incoming(6), back(legal))
    assert store.conversations[901].state == "discovering_need"
    assert store.conversations[901].need is None

    welcome = await service.clear(incoming(7))
    assert all(not choice.id.startswith("back:") for choice in welcome.choices)
    await service.handle_callback(incoming(8), back(legal))
    assert store.conversations[901].state == "greeting"


@pytest.mark.asyncio
async def test_support_catalog_displays_current_wording_and_keeps_legacy_callbacks() -> None:
    service = ConversationService(InMemoryConversationStore())
    await service.start(incoming(1))
    await service.handle_callback(incoming(2), "continue")
    turn = await service.handle_callback(incoming(3), "need:support")
    labels = {choice.id: choice.label for choice in turn.choices}
    assert labels["aid:psychologist_3_sessions"] == "Встреча с психологом"
    assert labels["aid:peer_consultation"] == "Разговор с женщиной, пережившей схожий опыт"
    assert "Три" not in turn.text


@pytest.mark.asyncio
async def test_outbox_persists_the_same_back_button_that_the_user_received() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store)
    turn = await begin(service)
    record = store.conversations[901]
    saved = await store.load_text_outcome(record, InboundExecutionKey.callback(3))
    assert saved is not None
    assert back(saved[0]) == back(turn)


@pytest.mark.asyncio
async def test_back_restores_contact_value_step_with_its_selected_method() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store)
    await begin(service)
    await service.handle_callback(incoming(4), "aid:legal_consultation")
    await service.handle_callback(incoming(5), "contact:email")
    handoff = await service.handle_callback(incoming(6), "human")
    alerts = copy.deepcopy(store.escalations)
    restored = await service.handle_callback(incoming(7), back(handoff))
    assert store.conversations[901].state == "collecting_contact_value"
    assert store.conversations[901].pending_contact_method == "email"
    assert "email" in restored.text
    assert store.escalations == alerts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "callback"),
    [
        ("followup_sent", "followup:better"),
        ("followup_answered", "level2:details"),
        ("safety_escalation", "continue_bot"),
    ],
)
async def test_back_restores_non_catalog_states_with_working_controls(
    state: str, callback: str
) -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(incoming(1))
    await store.update(record, state=state)
    service = ConversationService(store)
    welcome = await service.start(incoming(2))
    turn = await service.handle_callback(incoming(3), back(welcome))
    assert record.state == state
    assert any(choice.id == callback for choice in turn.choices)


@pytest.mark.asyncio
async def test_old_back_after_background_transition_refreshes_before_navigating() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store)
    await begin(service)
    await service.handle_callback(incoming(4), "aid:legal_consultation")
    done = await service.handle_callback(incoming(5), "contact:later")
    record = store.conversations[901]
    # The follow-up worker writes this state directly in its own transaction.
    record.state = "followup_sent"
    refreshed = await service.handle_callback(incoming(6), back(done))
    assert record.state == "followup_sent"
    assert any(choice.id == "followup:better" for choice in refreshed.choices)
    await service.handle_callback(incoming(7), back(refreshed))
    assert record.state == "aid_requested"

"""Ownership matrix: native UI controls, commands, restart and event races."""

from copy import deepcopy

import pytest

from app.chatwoot.contracts import ConversationChanged, StaffMessage
from app.chatwoot.service import ChatwootAgentService
from tests.test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation


@pytest.mark.parametrize("version", [None, 1, 2, 3])
@pytest.mark.parametrize("status", ["open", "pending", "resolved", "snoozed"])
async def test_unassigned_conversation_recovers_from_every_legacy_owner(version, status):
    api = FakeChatwoot()
    api.conversation.update(status=status, assignee_team_id=9)
    api.conversation["custom_attributes"].update(
        reply_owner="human", ownership_version=version, handoff_requested=True,
        scenario_requests={"keep": {"state": "requested"}},
    )
    messages = api.messages
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    assert await service.process(event())
    attrs = api.conversation["custom_attributes"]
    assert attrs["reply_owner"] == "bot" and attrs["ownership_version"] == 3
    assert attrs["handoff_requested"] and attrs["scenario_requests"]["keep"]["state"] == "requested"
    assert api.messages == messages and api.conversation["assignee_team_id"] == 9
    assert len(api.replies) == 1


@pytest.mark.parametrize("status", ["open", "pending", "snoozed"])
@pytest.mark.parametrize("command", ["/start", "/clear", "/system_info", "human", "continue"])
async def test_commands_and_old_buttons_explain_human_mode_without_stealing_it(status, command):
    api = FakeChatwoot()
    api.conversation.update(status=status, assignee_id=4)
    api.conversation["custom_attributes"]["scenario_requests"] = {"keep": {"state": "requested"}}
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)
    assert await service.process(event(command))
    assert not await service.process(event(command))  # delivery retry
    assert not await service.process(event("test input", 42))
    assert api.conversation["assignee_id"] == 4
    assert api.conversation["custom_attributes"]["reply_owner"] == "human"
    assert api.conversation["custom_attributes"]["scenario_requests"]["keep"]["state"] == "requested"
    assert "специалист" in api.replies[-1]["text"]
    assert gateway.calls == 0 and len(api.replies) == 1


async def test_system_info_does_not_answer_survey_or_advance_workflow():
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(
        reply_owner="bot", ownership_version=3,
        scenario={"screen": "test-screen"},
        scenario_followups={"keep": {"state": "sent"}},
    )
    before = deepcopy(api.conversation["custom_attributes"])
    assert await ChatwootAgentService(api).process(event("/system_info"))
    assert api.conversation["custom_attributes"] == before
    assert "Отвечает: бот" in api.replies[-1]["text"]


async def test_unassignment_recovers_after_restart_even_without_change_webhook():
    api = FakeChatwoot()
    service = ChatwootAgentService(api)
    await service.process(StaffMessage(40, 23, 4))
    assert api.conversation["meta"]["assignee"]["id"] == 4
    assert not await service.process(event())
    await api.unassign_human(23)
    restarted = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    assert await restarted.process(event("test input", 42))
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"


async def test_staff_reply_does_not_reassign_an_existing_specialist():
    api = FakeChatwoot()
    api.conversation["assignee_id"] = 5
    await ChatwootAgentService(api).process(StaffMessage(40, 23, 4))
    assert api.conversation["assignee_id"] == 5


async def test_native_close_releases_owner_but_does_not_complete_requests():
    api = FakeChatwoot()
    api.conversation.update(status="resolved", assignee_id=4)
    requests = {"keep": {"state": "requested"}}
    api.conversation["custom_attributes"].update(
        reply_owner="human", ownership_version=3, scenario_requests=deepcopy(requests),
    )
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    await service.process(ConversationChanged(23))
    assert api.conversation["status"] == "resolved"
    assert not api.conversation["assignee_id"] and not api.replies
    assert api.conversation["custom_attributes"]["scenario_requests"] == requests
    assert await service.process(event("test input", 43))
    assert api.conversation["status"] == "pending"


async def test_delayed_staff_webhook_cannot_undo_native_unassignment():
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(reply_owner="human", ownership_version=3)
    api.messages += ({"id": 40, "message_type": 1, "private": False,
                      "sender": {"id": 4, "type": "user"}, "content": "staff reply"},)
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    await service.process(ConversationChanged(23))
    await service.process(StaffMessage(40, 23, 4))
    assert not api.conversation["assignee_id"]
    assert await service.process(event())


async def test_delayed_staff_reply_cannot_undo_explicit_return_or_newer_bot_turn():
    api = FakeChatwoot()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    await service.process(StaffMessage(39, 23, 4, return_to_bot=True))
    await service.process(StaffMessage(38, 23, 4))
    assert await service.process(event())
    await service.process(StaffMessage(40, 23, 4))
    assert not api.conversation["assignee_id"]
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"


async def test_staff_reply_during_llm_prevents_late_reply_even_before_its_webhook():
    api = FakeChatwoot()

    class ReplyingGateway(StubGateway):
        async def evaluate(self, context):
            api.messages += ({"id": 42, "message_type": 1, "private": False,
                              "sender": {"id": 4, "type": "user"}, "content": "staff reply"},)
            return self.result

    service = ChatwootAgentService(api, gateway=ReplyingGateway(ordinary_evaluation()))
    assert not await service.process(event())
    assert not api.replies
    assert api.conversation["assignee_id"] == 4
    assert api.conversation["custom_attributes"].get("scenario_pending_input") is None


async def test_human_assignment_then_release_does_not_leave_inflight_workflow():
    api = FakeChatwoot()

    class ClaimingGateway(StubGateway):
        async def evaluate(self, context):
            api.conversation["assignee_id"] = 4
            return self.result

    service = ChatwootAgentService(api, gateway=ClaimingGateway(ordinary_evaluation()))
    assert not await service.process(event())
    assert not api.conversation["custom_attributes"].get("scenario_pending_input")
    await api.unassign_human(23)
    assert await ChatwootAgentService(api).process(event("/start", 43))


async def test_late_status_webhook_uses_current_assignment_not_stale_event_state():
    api = FakeChatwoot()
    api.conversation.update(status="open", assignee_id=4)
    await ChatwootAgentService(api).process(ConversationChanged(23))
    assert api.conversation["custom_attributes"]["reply_owner"] == "human"


async def test_old_clear_cannot_reset_newer_processed_input():
    api = FakeChatwoot()
    service = ChatwootAgentService(api)
    assert await service.process(event("/start", 43))
    before = deepcopy(api.conversation["custom_attributes"])
    assert not await service.process(event("/clear", 41))
    assert api.conversation["custom_attributes"] == before

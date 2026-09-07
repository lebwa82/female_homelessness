"""Technical queue/ownership regressions, independent of sensitive dialogue fixtures."""

from copy import deepcopy
from dataclasses import dataclass, field

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation

from app.chatwoot.contracts import (
    RETURN_TO_BOT,
    ConversationChanged,
    StaffMessage,
    parse_staff_message,
)
from app.chatwoot.routing import RouteDecision, Team, validate_decision
from app.chatwoot.service import ChatwootAgentService


@dataclass
class CatalogApi(FakeChatwoot):
    catalog: list = field(default_factory=lambda: [
        {"id": 9, "name": "Duty", "description": "General"},
        {"id": 10, "name": "Legal", "description": "Documents and rights"},
    ])
    members: dict = field(default_factory=lambda: {9: (4,), 10: (4, 5)})

    async def get_teams(self):
        return tuple(self.catalog)

    async def get_team_members(self, team_id):
        return self.members.get(team_id, ())


@dataclass
class SelectingRouter:
    selected: int = 10
    catalogs: list = field(default_factory=list)

    async def choose(self, history, teams, default_id):
        self.catalogs.append(teams)
        return RouteDecision(self.selected)


def service(api, router=None):
    return ChatwootAgentService(
        api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9,
        queue_router=router or SelectingRouter(),
    )


async def test_model_route_assigns_team_and_mentions_all_members_without_takeover():
    api = CatalogApi()
    bot = service(api)
    assert await bot.process(event("human"))
    assert api.teams == [10]
    assert "mention://team/10/queue" in api.notes[0]
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"
    assert not api.conversation["assignee_id"]
    assert await bot.process(event("test followup", 42))


async def test_catalog_changes_and_renames_are_loaded_without_restart():
    api, router = CatalogApi(), SelectingRouter()
    bot = service(api, router)
    await bot.process(event("human"))
    api.catalog[1]["name"] = "Renamed"
    api.catalog.append({"id": 11, "name": "New", "description": "New specialty"})
    api.members[11] = (6,)
    router.selected = 11
    await bot.process(event("human", 42))
    assert [t.name for t in router.catalogs[-1]] == ["Duty", "Renamed", "New"]
    assert api.teams == [10, 11]


async def test_empty_or_unknown_queue_falls_back_to_duty():
    api, router = CatalogApi(), SelectingRouter(selected=10)
    api.members[10] = ()
    await service(api, router).process(event("human"))
    assert [t.id for t in router.catalogs[0]] == [9]
    assert api.teams == [9]


async def test_manual_transfer_notifies_once_and_survives_restart_without_bot_reroute():
    api, router = CatalogApi(), SelectingRouter()
    bot = service(api, router)
    await bot.process(event("human"))
    api.conversation["assignee_team_id"] = 9
    await bot.process(ConversationChanged(23))
    await bot.process(ConversationChanged(23))
    assert len(api.notes) == 2
    await service(api, router).process(event("human", 43))
    assert len(router.catalogs) == 1
    assert api.conversation["assignee_team_id"] == 9


async def test_manual_transfer_notification_retries_without_duplicate():
    class FailAfterNote(CatalogApi):
        fail = True

        async def add_private_note(self, conversation_id, content, *, event_key=None):
            await super().add_private_note(conversation_id, content, event_key=event_key)
            if self.fail:
                self.fail = False
                raise ConnectionError("lost response")

    api = FailAfterNote()
    api.conversation["assignee_team_id"] = 10
    bot = service(api)
    with pytest.raises(ConnectionError):
        await bot.process(ConversationChanged(23))
    await service(api).process(ConversationChanged(23))
    assert len(api.notes) == 1
    assert api.conversation["custom_attributes"]["routing_notice"]["sent"]


async def test_failed_assignment_retries_saved_selection_not_a_new_model_decision():
    class FailAssignment(CatalogApi):
        fail = True

        async def assign_team(self, conversation_id, team_id):
            if self.fail:
                self.fail = False
                raise ConnectionError("temporary")
            await super().assign_team(conversation_id, team_id)

    api, router = FailAssignment(), SelectingRouter()
    with pytest.raises(ConnectionError):
        await service(api, router).process(event("human"))
    router.selected = 9
    assert await service(api, router).process(event("human"))
    assert api.teams == [10]
    assert len(router.catalogs) == 1
    assert len(api.notes) == 1


async def test_takeover_and_transfer_keep_bot_silent_until_explicit_return():
    api = CatalogApi()
    bot = service(api)
    await bot.process(event("human"))
    api.conversation["assignee_id"] = 4
    await bot.process(ConversationChanged(23))
    api.conversation.update(assignee_id=None, assignee_team_id=9)
    await bot.process(ConversationChanged(23))
    bot = service(api)  # No process-local ownership state.
    assert not await bot.process(event("test", 42))
    assert await bot.process(event("/clear", 43))
    assert not await bot.process(event("test", 44))
    previous = deepcopy(api.conversation["custom_attributes"])
    await bot.process(StaffMessage(50, 23, 4, return_to_bot=True))
    assert await bot.process(event("test", 51))
    assert api.conversation["custom_attributes"]["context_epoch"] == previous["context_epoch"]
    assert api.conversation["custom_attributes"]["handoff_requested"]
    assert api.conversation["assignee_team_id"] == 9
    await bot.process(StaffMessage(49, 23, 4))  # Out-of-order old staff message.
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"


async def test_staff_reply_without_assignment_also_latches_human_mode():
    api = CatalogApi()
    bot = service(api)
    await bot.process(StaffMessage(48, 23, 4))
    assert not await bot.process(event("test", 49))


async def test_human_takeover_during_queue_model_call_wins():
    api = CatalogApi()

    class ClaimingRouter(SelectingRouter):
        async def choose(self, history, teams, default_id):
            api.conversation["assignee_id"] = 4
            return RouteDecision(10)

    assert not await service(api, ClaimingRouter()).process(event("human"))
    assert api.teams == [] and api.notes == [] and api.replies == []


async def test_manual_transfer_during_queue_model_call_wins():
    api = CatalogApi()

    class MovingRouter(SelectingRouter):
        async def choose(self, history, teams, default_id):
            api.conversation["assignee_team_id"] = 9
            return RouteDecision(10)

    assert await service(api, MovingRouter()).process(event("human"))
    assert api.teams == []
    assert "mention://team/9/queue" in api.notes[0]


@pytest.mark.parametrize("payload", [
    {"team_id": 88, "confidence": 1}, {"team_id": "10", "confidence": 1},
    {"team_id": 10, "confidence": 0.3}, {"team_id": True, "confidence": 1}, {},
])
def test_invalid_model_selection_never_creates_an_arbitrary_route(payload):
    teams = (Team(9, "Duty"), Team(10, "Legal"))
    assert validate_decision(payload, teams, 9).team_id == 9


@pytest.mark.parametrize("private,sender_type,message_type,allowed", [
    (True, "user", "outgoing", True),
    (False, "user", "outgoing", False),
    (True, "contact", "incoming", False),
    (True, "agent_bot", "outgoing", False),
    (True, "user", "incoming", False),
])
def test_only_private_staff_command_can_return_bot(private, sender_type, message_type, allowed):
    parsed = parse_staff_message({
        "event": "message_created", "id": 50, "conversation": {"id": 23},
        "sender": {"id": 4, "type": sender_type}, "private": private,
        "message_type": message_type, "content": RETURN_TO_BOT,
    })
    assert bool(parsed and parsed.return_to_bot) is allowed

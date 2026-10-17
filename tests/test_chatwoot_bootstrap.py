from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from deploy.chatwoot.bootstrap import (
    AgentBotBootstrap,
    AiohttpChatwootAdminApi,
    BootstrapApiError,
    BootstrapSettings,
)


@dataclass
class FakeChatwootAdminApi:
    teams: list[dict[str, Any]] = field(default_factory=list)
    bots: list[dict[str, Any]] = field(default_factory=list)
    attached: list[tuple[int, int]] = field(default_factory=list)

    async def list_teams(self) -> list[dict[str, Any]]:
        return self.teams

    async def create_team(self, name: str) -> dict[str, Any]:
        team = {"id": 8, "name": name.strip().lower()}
        self.teams.append(team)
        return team

    async def list_agent_bots(self) -> list[dict[str, Any]]:
        return self.bots

    async def get_agent_bot(self, agent_bot_id: int) -> dict[str, Any]:
        return next(bot for bot in self.bots if bot["id"] == agent_bot_id)

    async def create_agent_bot(self, *, name: str, outgoing_url: str) -> dict[str, Any]:
        bot = {"id": 9, "name": name, "outgoing_url": outgoing_url, "access_token": "bot-token"}
        self.bots.append(bot)
        return bot

    async def attach_agent_bot(self, *, inbox_id: int, agent_bot_id: int) -> None:
        self.attached.append((inbox_id, agent_bot_id))


def settings() -> BootstrapSettings:
    return BootstrapSettings(
        account_id=3,
        inbox_id=5,
        team_name="Duty team",
        bot_name="Women Help Agent",
        outgoing_url="https://agent.example.test/webhooks/chatwoot/agent/secret",
        read_token="read-token",
    )


def test_bootstrap_transport_disables_content_encoding_for_the_pinned_release() -> None:
    assert AiohttpChatwootAdminApi.request_headers("read-token") == {
        "Api-Access-Token": "read-token",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("team_name", ["Duty team", "Дежурные"])
async def test_bootstrap_creates_team_bot_and_inbox_attachment_idempotently(team_name: str) -> None:
    api = FakeChatwootAdminApi()
    bootstrap = AgentBotBootstrap(api, replace(settings(), team_name=team_name))

    first = await bootstrap.provision()
    second = await bootstrap.provision()

    assert first == second
    assert first.team_id == 8
    assert first.agent_bot_id == 9
    assert first.bot_token == "bot-token"
    assert api.attached == [(5, 9), (5, 9)]
    assert len(api.teams) == 1
    assert len(api.bots) == 1


@pytest.mark.asyncio
async def test_bootstrap_rejects_an_existing_bot_with_another_webhook_url() -> None:
    api = FakeChatwootAdminApi(
        bots=[
            {
                "id": 9,
                "name": "Women Help Agent",
                "outgoing_url": "https://other.example.test/webhooks",
                "access_token": "bot-token",
            }
        ]
    )

    with pytest.raises(ValueError, match="Agent Bot URL does not match"):
        await AgentBotBootstrap(api, settings()).provision()


@pytest.mark.asyncio
async def test_agent_attachment_uses_native_parameter_and_verifies_readback() -> None:
    api = AiohttpChatwootAdminApi(base_url="http://example.test", account_id=3, access_token="test-token")
    api._request = AsyncMock(side_effect=[{}, {"agent_bot": {"id": 9}}])

    await api.attach_agent_bot(inbox_id=5, agent_bot_id=9)

    assert api._request.await_args_list == [
        call("POST", "/api/v1/accounts/3/inboxes/5/set_agent_bot", {"agent_bot": 9}),
        call("GET", "/api/v1/accounts/3/inboxes/5/agent_bot"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("attached", [None, {}, {"id": 4}])
async def test_agent_attachment_rejects_success_without_matching_binding(attached) -> None:
    api = AiohttpChatwootAdminApi(base_url="http://example.test", account_id=3, access_token="test-token")
    api._request = AsyncMock(side_effect=[{}, {"agent_bot": attached}])

    with pytest.raises(BootstrapApiError, match="inbox Agent Bot attachment"):
        await api.attach_agent_bot(inbox_id=5, agent_bot_id=9)

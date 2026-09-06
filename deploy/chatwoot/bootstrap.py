"""Idempotent, secret-safe provisioning for the Chatwoot Agent Bot."""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import aiohttp


class BootstrapApiError(RuntimeError):
    """API failure with operation metadata only, never a response body."""


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    account_id: int
    inbox_id: int
    team_name: str
    bot_name: str
    outgoing_url: str
    read_token: str


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    team_id: int
    agent_bot_id: int
    bot_token: str


class ChatwootAdminApi(Protocol):
    async def list_teams(self) -> list[dict[str, Any]]: ...

    async def create_team(self, name: str) -> dict[str, Any]: ...

    async def list_agent_bots(self) -> list[dict[str, Any]]: ...

    async def get_agent_bot(self, agent_bot_id: int) -> dict[str, Any]: ...

    async def create_agent_bot(self, *, name: str, outgoing_url: str) -> dict[str, Any]: ...

    async def attach_agent_bot(self, *, inbox_id: int, agent_bot_id: int) -> None: ...


class AgentBotBootstrap:
    """Ensure the small Chatwoot control-plane topology exists exactly once."""

    def __init__(self, api: ChatwootAdminApi, settings: BootstrapSettings) -> None:
        self._api = api
        self._settings = settings

    async def provision(self) -> BootstrapResult:
        team = await self._find_or_create_team()
        bot = await self._find_or_create_bot()
        team_id = _required_id(team, "team")
        agent_bot_id = _required_id(bot, "agent bot")
        token = bot.get("access_token")
        if not isinstance(token, str) or not token:
            bot = await self._api.get_agent_bot(agent_bot_id)
            token = bot.get("access_token")
        if not isinstance(token, str) or not token:
            raise BootstrapApiError("agent bot token was not returned")
        await self._api.attach_agent_bot(
            inbox_id=self._settings.inbox_id,
            agent_bot_id=agent_bot_id,
        )
        return BootstrapResult(team_id=team_id, agent_bot_id=agent_bot_id, bot_token=token)

    async def _find_or_create_team(self) -> dict[str, Any]:
        for team in await self._api.list_teams():
            if team.get("name") == self._settings.team_name:
                return team
        return await self._api.create_team(self._settings.team_name)

    async def _find_or_create_bot(self) -> dict[str, Any]:
        for bot in await self._api.list_agent_bots():
            if bot.get("name") != self._settings.bot_name:
                continue
            if bot.get("outgoing_url") != self._settings.outgoing_url:
                raise ValueError("Agent Bot URL does not match the configured secure webhook route")
            return bot
        return await self._api.create_agent_bot(
            name=self._settings.bot_name,
            outgoing_url=self._settings.outgoing_url,
        )


class AiohttpChatwootAdminApi:
    """Application API boundary for first-time provisioning only."""

    def __init__(self, *, base_url: str, account_id: int, access_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._access_token = access_token
        self._timeout = aiohttp.ClientTimeout(total=15)

    def _path(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self._account_id}{suffix}"

    @staticmethod
    def request_headers(token: str) -> dict[str, str]:
        return {
            "api_access_token": token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    async def list_teams(self) -> list[dict[str, Any]]:
        return _items(await self._request("GET", self._path("/teams")))

    async def create_team(self, name: str) -> dict[str, Any]:
        return _object(await self._request("POST", self._path("/teams"), {"name": name}))

    async def list_agent_bots(self) -> list[dict[str, Any]]:
        return _items(await self._request("GET", self._path("/agent_bots")))

    async def get_agent_bot(self, agent_bot_id: int) -> dict[str, Any]:
        return _object(await self._request("GET", self._path(f"/agent_bots/{agent_bot_id}")))

    async def create_agent_bot(self, *, name: str, outgoing_url: str) -> dict[str, Any]:
        return _object(
            await self._request(
                "POST",
                self._path("/agent_bots"),
                {
                    "name": name,
                    "description": "Women Help policy Agent Bot",
                    "outgoing_url": outgoing_url,
                },
            )
        )

    async def attach_agent_bot(self, *, inbox_id: int, agent_bot_id: int) -> None:
        await self._request(
            "POST",
            self._path(f"/inboxes/{inbox_id}/set_agent_bot"),
            {"agent_bot_id": agent_bot_id},
        )

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as session,
                session.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self.request_headers(self._access_token),
                    json=payload,
                ) as response,
            ):
                if response.status >= 400:
                    raise BootstrapApiError(f"Chatwoot API {method} {path} failed: {response.status}")
                if response.status == 204:
                    return {}
                return await response.json(content_type=None)
        except BootstrapApiError:
            raise
        except aiohttp.ClientError as error:
            raise BootstrapApiError(f"Chatwoot API {method} {path} failed") from error


def _required_id(value: dict[str, Any], name: str) -> int:
    candidate = value.get("id")
    if isinstance(candidate, bool):
        candidate = None
    try:
        result = int(candidate)
    except (TypeError, ValueError) as error:
        raise BootstrapApiError(f"{name} ID was not returned") from error
    if result <= 0:
        raise BootstrapApiError(f"{name} ID was not returned")
    return result


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BootstrapApiError("Chatwoot API returned an invalid object")
    return value


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("payload", value.get("data", []))
    if not isinstance(value, list):
        raise BootstrapApiError("Chatwoot API returned an invalid list")
    return [item for item in value if isinstance(item, dict)]


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key.replace("_", "").isalnum():
            values[key] = value
    return values


def _required(values: dict[str, str], name: str) -> str:
    value = values.get(name, "")
    if not value:
        raise BootstrapApiError(f"missing {name}")
    return value


def _write_runtime_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            for key in sorted(values):
                destination.write(f"{key}={values[key]}\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def _run(agent_env_path: Path, chatwoot_env_path: Path) -> BootstrapResult:
    agent_values = _read_env(agent_env_path)
    chatwoot_values = _read_env(chatwoot_env_path)
    account_id = int(_required(agent_values, "CHATWOOT_ACCOUNT_ID"))
    inbox_id = int(_required(agent_values, "CHATWOOT_INBOX_ID"))
    base_url = _required(agent_values, "CHATWOOT_BASE_URL")
    read_token = _required(agent_values, "CHATWOOT_READ_TOKEN")
    webhook_secret = _required(chatwoot_values, "CHATWOOT_WEBHOOK_SECRET")
    agent_hostname = _required(chatwoot_values, "AGENT_HOSTNAME")
    settings = BootstrapSettings(
        account_id=account_id,
        inbox_id=inbox_id,
        team_name=agent_values.get("CHATWOOT_DUTY_TEAM_NAME", "Дежурные"),
        bot_name="Women Help Agent",
        outgoing_url=f"https://{agent_hostname}/webhooks/chatwoot/agent/{webhook_secret}",
        read_token=read_token,
    )
    api = AiohttpChatwootAdminApi(
        base_url=base_url,
        account_id=settings.account_id,
        access_token=settings.read_token,
    )
    result = await AgentBotBootstrap(api, settings).provision()
    agent_values.update(
        {
            "CHATWOOT_ACCOUNT_ID": str(settings.account_id),
            "CHATWOOT_BASE_URL": base_url,
            "CHATWOOT_BOT_TOKEN": result.bot_token,
            "CHATWOOT_DUTY_TEAM_ID": str(result.team_id),
        }
    )
    agent_values.pop("CHATWOOT_INBOX_ID", None)
    agent_values.pop("CHATWOOT_DUTY_TEAM_NAME", None)
    _write_runtime_env(agent_env_path, agent_values)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the Women Help Chatwoot Agent Bot")
    parser.add_argument("--agent-env", type=Path, required=True)
    parser.add_argument("--chatwoot-env", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(_run(args.agent_env, args.chatwoot_env))
    print(f"Chatwoot Agent Bot configured: team={result.team_id}; agent_bot={result.agent_bot_id}")


if __name__ == "__main__":
    main()

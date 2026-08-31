"""Narrow Chatwoot Application API client used by the stateless Agent Bot."""

from __future__ import annotations

from typing import Any, Protocol

import aiohttp

from app.domain import Choice


class ChatwootApiError(RuntimeError):
    """A safe API error: it carries operation metadata, never response text."""

    def __init__(self, operation: str, status: int) -> None:
        super().__init__(f"chatwoot_{operation}_failed:{status}")
        self.operation = operation
        self.status = status


class ChatwootTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> Any: ...


class AiohttpChatwootTransport:
    """HTTP-only transport kept separate from product conversation logic."""

    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    @staticmethod
    def request_headers(token: str) -> dict[str, str]:
        # Chatwoot v4.12.1 self-hosted has an authentication regression for
        # compressed API requests. Its Agent Bot implementation is otherwise
        # the supported test-stack choice, so keep every API call uncompressed.
        return {
            "api_access_token": token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as session,
                session.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self.request_headers(token),
                    json=payload,
                ) as response,
            ):
                if response.status >= 400:
                    raise ChatwootApiError(path.rsplit("/", 1)[-1], response.status)
                if response.status == 204:
                    return {}
                return await response.json(content_type=None)
        except ChatwootApiError:
            raise
        except aiohttp.ClientError as error:
            raise ChatwootApiError(path.rsplit("/", 1)[-1], 0) from error


class ChatwootClient:
    """Chatwoot boundary with distinct read and Agent Bot identities."""

    def __init__(
        self,
        *,
        base_url: str,
        account_id: int,
        read_token: str,
        bot_token: str,
        transport: ChatwootTransport | None = None,
    ) -> None:
        self._account_id = account_id
        self._read_token = read_token
        self._bot_token = bot_token
        self._transport = transport or AiohttpChatwootTransport(base_url)

    def _path(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self._account_id}{suffix}"

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        payload = await self._transport.request(
            "GET", self._path(f"/conversations/{conversation_id}"), self._read_token
        )
        return _as_object(payload)

    async def get_messages(self, conversation_id: int) -> tuple[dict[str, Any], ...]:
        payload = await self._transport.request(
            "GET", self._path(f"/conversations/{conversation_id}/messages"), self._read_token
        )
        return _messages_from_payload(payload)

    async def set_custom_attributes(self, conversation_id: int, attributes: dict[str, Any]) -> None:
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/custom_attributes"),
            self._bot_token,
            {"custom_attributes": attributes},
        )

    async def set_status(self, conversation_id: int, status: str) -> None:
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/toggle_status"),
            self._bot_token,
            {"status": status},
        )

    async def assign_team(self, conversation_id: int, team_id: int) -> None:
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/assignments"),
            self._bot_token,
            {"assignee_team_id": team_id},
        )

    async def add_private_note(self, conversation_id: int, content: str) -> None:
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/messages"),
            self._bot_token,
            {"content": content, "message_type": "outgoing", "private": True},
        )

    async def send_reply(
        self,
        conversation_id: int,
        *,
        text: str,
        choices: tuple[Choice, ...],
        turn_key: str,
    ) -> None:
        content_attributes: dict[str, Any] = {"bot_turn_key": turn_key}
        payload: dict[str, Any] = {
            "content": text,
            "message_type": "outgoing",
            "private": False,
            "content_attributes": content_attributes,
        }
        if choices:
            content_attributes["items"] = [
                {"title": choice.label, "value": choice.id} for choice in choices
            ]
            payload["content_type"] = "input_select"
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/messages"),
            self._bot_token,
            payload,
        )

    async def has_reply_for_turn(self, conversation_id: int, turn_key: str) -> bool:
        messages = await self.get_messages(conversation_id)
        return any(
            isinstance(message.get("content_attributes"), dict)
            and message["content_attributes"].get("bot_turn_key") == turn_key
            for message in messages
        )


def _as_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ChatwootApiError("invalid_payload", 200)
    return payload


def _messages_from_payload(payload: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(payload, dict):
        candidates = payload.get("payload", payload.get("messages", ()))
    else:
        candidates = payload
    if not isinstance(candidates, list):
        raise ChatwootApiError("invalid_messages_payload", 200)
    return tuple(item for item in candidates if isinstance(item, dict))

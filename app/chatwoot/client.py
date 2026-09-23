"""Narrow Chatwoot Application API client used by the stateless Agent Bot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from app.domain import Choice


class ChatwootApiError(RuntimeError):
    """A safe API error: it carries operation metadata, never response text."""

    def __init__(self, operation: str, status: int) -> None:
        super().__init__(f"chatwoot_{operation}_failed:{status}")
        self.operation = operation
        self.status = status


@dataclass(frozen=True, slots=True)
class BinaryAttachment:
    filename: str
    content_type: str
    data: bytes


class ChatwootTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> Any: ...

    async def request_multipart(
        self,
        method: str,
        path: str,
        token: str,
        fields: dict[str, str],
        attachment: BinaryAttachment,
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

    async def request_multipart(
        self,
        method: str,
        path: str,
        token: str,
        fields: dict[str, str],
        attachment: BinaryAttachment,
    ) -> Any:
        form = aiohttp.FormData()
        for name, value in fields.items():
            form.add_field(name, value)
        form.add_field(
            "attachments[]",
            attachment.data,
            filename=attachment.filename,
            content_type=attachment.content_type,
        )
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as session,
                session.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self.request_headers(token),
                    data=form,
                ) as response,
            ):
                if response.status >= 400:
                    raise ChatwootApiError(path.rsplit("/", 1)[-1], response.status)
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

    async def get_teams(self) -> tuple[dict[str, Any], ...]:
        payload = await self._transport.request("GET", self._path("/teams"), self._read_token)
        if not isinstance(payload, list):
            raise ChatwootApiError("invalid_teams", 200)
        return tuple(item for item in payload if isinstance(item, dict))

    async def get_team_members(self, team_id: int) -> tuple[int, ...]:
        payload = await self._transport.request(
            "GET", self._path(f"/teams/{team_id}/team_members"), self._read_token
        )
        if not isinstance(payload, list):
            raise ChatwootApiError("invalid_team_members", 200)
        return tuple(m["id"] for m in payload if isinstance(m, dict) and type(m.get("id")) is int)

    async def unassign_human(self, conversation_id: int) -> None:
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/assignments"),
            self._bot_token,
            {"assignee_id": None},
        )

    async def get_messages(self, conversation_id: int) -> tuple[dict[str, Any], ...]:
        path = self._path(f"/conversations/{conversation_id}/messages")
        messages: dict[int, dict[str, Any]] = {}
        before: int | None = None
        while True:
            suffix = f"?before={before}" if before is not None else ""
            payload = await self._transport.request("GET", path + suffix, self._read_token)
            page = _messages_from_payload(payload)
            # Chatwoot's messages endpoint returns 20 records per page.
            if len(page) < 20:
                return tuple(messages.values()) + page
            ids = [message["id"] for message in page]
            next_before = min(ids)
            if before is not None and next_before >= before:
                raise ChatwootApiError("message_pagination_stalled", 200)
            messages.update({message["id"]: message for message in page})
            before = next_before

    async def set_custom_attributes(self, conversation_id: int, attributes: dict[str, Any]) -> None:
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/custom_attributes"),
            self._bot_token,
            {"custom_attributes": attributes, "merge": True},
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
            {"team_id": team_id},
        )

    async def add_private_note(
        self, conversation_id: int, content: str, *, event_key: str | None = None
    ) -> None:
        if event_key and any(
            m.get("content_attributes", {}).get("bot_event_key") == event_key
            for m in await self.get_messages(conversation_id)
        ):
            return
        payload = {"content": content, "message_type": "outgoing", "private": True}
        if event_key:
            payload["content_attributes"] = {"bot_event_key": event_key}
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/messages"),
            self._bot_token,
            payload,
        )

    async def send_reply(
        self,
        conversation_id: int,
        *,
        text: str,
        choices: tuple[Choice, ...],
        turn_key: str,
        sensitive_content: str | None = None,
        attachment: BinaryAttachment | None = None,
    ) -> int | None:
        content_attributes: dict[str, Any] = {"bot_turn_key": turn_key}
        if sensitive_content:
            content_attributes["bot_sensitive_content"] = sensitive_content
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
        path = self._path(f"/conversations/{conversation_id}/messages")
        if attachment is None:
            response = await self._transport.request("POST", path, self._bot_token, payload)
        else:
            fields = {
                "content": text,
                "message_type": "outgoing",
                "private": "false",
                "content_attributes": json.dumps(content_attributes, ensure_ascii=False),
            }
            response = await self._transport.request_multipart(
                "POST", path, self._bot_token, fields, attachment
            )
        return _message_id(response)

    async def has_reply_for_turn(self, conversation_id: int, turn_key: str) -> bool:
        messages = await self.get_messages(conversation_id)
        return any(
            isinstance(message.get("content_attributes"), dict)
            and message["content_attributes"].get("bot_turn_key") == turn_key
            for message in messages
        )

    async def reply_id_for_turn(self, conversation_id: int, turn_key: str) -> int | None:
        messages = await self.get_messages(conversation_id)
        match = next((
            message
            for message in messages
            if isinstance(message.get("content_attributes"), dict)
            and message["content_attributes"].get("bot_turn_key") == turn_key
        ), None)
        return _message_id(match)


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


def _message_id(payload: Any) -> int | None:
    if isinstance(payload, dict) and type(payload.get("id")) is int:
        return payload["id"]
    return None

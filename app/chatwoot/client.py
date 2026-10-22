"""Narrow Chatwoot Application API client used by the stateless Agent Bot."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from app.chatwoot.fast_buttons import (
    BUTTON_ATTRIBUTES,
    UnsupportedFastButton,
    validate_button_notes,
)
from app.chatwoot.telemetry import api_timing, reply_persisted
from app.domain import Choice


class ChatwootApiError(RuntimeError):
    """A safe API error: it carries operation metadata, never response text."""

    def __init__(self, operation: str, status: int) -> None:
        super().__init__(f"chatwoot_{operation}_failed:{status}")
        self.operation = operation
        self.status = status


class StaleButtonVersion(ChatwootApiError):
    """The native atomic commit explicitly rejected a stale read before writes."""

    def __init__(self) -> None:
        super().__init__("atomic_turn_stale_version", 409)


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

    def __init__(
        self, base_url: str, timeout_seconds: float = 10.0, *, reuse_connections: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._reuse_connections = reuse_connections
        self._session: aiohttp.ClientSession | None = None

    @asynccontextmanager
    async def _http_session(self):
        # Short-lived callers retain automatic cleanup. The service explicitly
        # owns its pool and closes it after all webhook/followup workers stop.
        # No shared cookies: read and Agent Bot identities stay request-local.
        if self._reuse_connections:
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=self._timeout, cookie_jar=aiohttp.DummyCookieJar(),
                )
            yield self._session
        else:
            async with aiohttp.ClientSession(
                timeout=self._timeout, cookie_jar=aiohttp.DummyCookieJar(),
            ) as session:
                yield session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()

    @staticmethod
    def request_headers(token: str) -> dict[str, str]:
        # Chatwoot v4.12.1 self-hosted has an authentication regression for
        # compressed API requests. Its Agent Bot implementation is otherwise
        # the supported test-stack choice, so keep every API call uncompressed.
        return {
            # Rails accepts the canonical Rack header. Proxies can discard
            # underscore-bearing names before the request reaches Chatwoot.
            "Api-Access-Token": token,
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
        with api_timing(method, path) as timing:
            try:
                async with (
                    self._http_session() as session,
                    session.request(
                        method,
                        f"{self._base_url}{path}",
                        headers=self.request_headers(token),
                        json=payload,
                    ) as response,
                ):
                    timing.status = response.status
                    timing.request_id = response.headers.get("X-Request-Id", "")
                    if (response.status == 409 and method == "POST" and path.endswith("/messages")
                            and isinstance(payload, dict) and "nevidimy_atomic_turn" in payload):
                        # Retain no response text. Only this bounded, exact native
                        # no-write signal permits a fresh snapshot/replan.
                        try:
                            body = await response.content.readexactly(257)
                        except asyncio.IncompleteReadError as error:
                            body = error.partial
                        if len(body) <= 256:
                            try:
                                conflict = json.loads(body.decode("utf-8"), object_pairs_hook=list)
                            except (ValueError, UnicodeDecodeError):
                                conflict = None
                            expected = [("outcome", "conflict"), ("reason", "stale_version")]
                            if conflict in (expected, expected[::-1]):
                                raise StaleButtonVersion()
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
        with api_timing(method, path) as timing:
            try:
                async with (
                    self._http_session() as session,
                    session.request(
                        method,
                        f"{self._base_url}{path}",
                        headers=self.request_headers(token),
                        data=form,
                    ) as response,
                ):
                    timing.status = response.status
                    timing.request_id = response.headers.get("X-Request-Id", "")
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
        fast_buttons: bool = False,
    ) -> None:
        self._account_id = account_id
        self._read_token = read_token
        self._bot_token = bot_token
        self._transport = transport or AiohttpChatwootTransport(base_url)
        self.fast_buttons = fast_buttons

    def _path(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self._account_id}{suffix}"

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        payload = await self._transport.request(
            "GET", self._path(f"/conversations/{conversation_id}"), self._read_token
        )
        return _as_object(payload)

    async def button_snapshot(self, conversation_id: int, message_id: int) -> dict[str, Any]:
        return _as_object(await self._transport.request(
            "GET", self._path(
                f"/conversations/{conversation_id}?nevidimy_turn_state={message_id}"
            ), self._read_token,
        ))

    async def commit_button(self, conversation_id: int, message_id: int, version: str,
                            attributes: dict, reply: dict, *,
                            notes: list[dict[str, str]] | None = None) -> int | None:
        payload = _reply_payload(**reply)
        # A legacy-supported large workflow must not become a permanently
        # failing queued event. Reject incompatible plans before any POST.
        encoded = json.dumps(attributes, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if (not attributes.keys() <= BUTTON_ATTRIBUTES or len(encoded.encode()) > 262_144
                or not isinstance(reply["text"], str) or len(reply["text"].encode()) > 32_768
                or attributes.get("scenario_pending_input")
                or attributes.get("scenario_last_message_id") != message_id):
            raise UnsupportedFastButton("unsupported_commit_plan")
        payload["nevidimy_atomic_turn"] = {
            "input_id": message_id, "version": version, "attributes": attributes,
        }
        if notes is not None:
            validate_button_notes(notes)
            if notes:
                payload["nevidimy_atomic_turn"]["notes"] = notes
        try:
            result = _as_object(await self._transport.request(
                "POST", self._path(f"/conversations/{conversation_id}/messages"),
                self._bot_token, payload,
            ))
        except StaleButtonVersion:
            raise
        except ChatwootApiError as error:
            if error.status == 409:
                return None  # Proven conflict before any write; safe legacy reread.
            raise  # Unknown POST outcome must retry its stable turn key, not send again.
        if result.get("outcome") not in {"committed", "duplicate"}:
            raise ChatwootApiError("atomic_turn_shape", 0)
        message = _message_id(result)
        if message is None:
            raise ChatwootApiError("atomic_turn_message", 0)
        if result["outcome"] == "committed":
            reply_persisted(message)
        return message

    async def list_conversations(self, page: int) -> tuple[dict[str, Any], ...]:
        payload = await self._transport.request(
            "GET", self._path(f"/conversations?status=all&assignee_type=all&page={page}"),
            self._read_token,
        )
        data = _as_object(payload).get("data", {})
        if not isinstance(data, dict) or not isinstance(data.get("payload"), list):
            raise ChatwootApiError("list_conversations_shape", 0)
        return tuple(item for item in data["payload"] if isinstance(item, dict))

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
        await self.assign_human(conversation_id, None)

    async def assign_human(self, conversation_id: int, user_id: int | None) -> None:
        await self._transport.request(
            "POST",
            self._path(f"/conversations/{conversation_id}/assignments"),
            self._bot_token,
            {"assignee_id": user_id},
        )

    async def get_messages(
        self, conversation_id: int, *, after_id: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        path = self._path(f"/conversations/{conversation_id}/messages")
        if after_id is not None:
            payload = await self._transport.request(
                "GET", f"{path}?after={after_id}", self._read_token,
            )
            tail = _messages_from_payload(payload)
            # Installed Chatwoot's `after` query filters IDs server-side, orders
            # by created_at, and caps at 100. Never infer an ID watermark from
            # that ordering: a saturated tail falls back to the full reader.
            if len(tail) < 100:
                return tail
            return tuple(m for m in await self.get_messages(conversation_id) if m["id"] > after_id)
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
        payload = _reply_payload(text=text, choices=choices, turn_key=turn_key,
                                 sensitive_content=sensitive_content)
        content_attributes = payload["content_attributes"]
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
        message_id = _message_id(response)
        reply_persisted(message_id)
        return message_id

    async def has_reply_for_turn(
        self, conversation_id: int, turn_key: str, *,
        messages: tuple[dict[str, Any], ...] | None = None, after_id: int | None = None,
    ) -> bool:
        if messages is None:
            messages = await self.get_messages(conversation_id, after_id=after_id)
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

    async def wait_for_external_delivery(
        self,
        conversation_id: int,
        message_id: int,
        *,
        timeout_seconds: float = 60,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        conversation = await self.get_conversation(conversation_id)
        api_channel = (conversation.get("meta") or {}).get("channel") == "Channel::Api"
        api_only = False
        if api_channel:
            inbox = await self._transport.request(
                "GET", self._path(f"/inboxes/{conversation['inbox_id']}"), self._read_token,
            )
            api_only = not (inbox.get("webhook_url") or inbox.get("callback_webhook_url"))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            messages = await self.get_messages(conversation_id)
            message = next(
                (
                    item
                    for item in messages
                    if item.get("id") == message_id
                ),
                None,
            )
            if message is not None:
                if message.get("status") == "failed":
                    raise ChatwootApiError("external_delivery", 502)
                if (message.get("source_id") not in {None, ""}
                        or api_only and message.get("attachments")):
                    return
                # Custom API connectors cannot PATCH source_id in Chatwoot.
                # Their private acceptance receipt is written only after every
                # external text/file part has a provider-generated message ID.
                # Telegram continues to require its native source_id above.
                if api_channel and any(_connector_receipt(item, message_id) for item in messages):
                    return
            if loop.time() >= deadline:
                raise ChatwootApiError("external_delivery", 504)
            await asyncio.sleep(poll_interval_seconds)


def _reply_payload(*, text: str, choices: tuple[Choice, ...], turn_key: str,
                   sensitive_content: str | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {"bot_turn_key": turn_key}
    if sensitive_content:
        attributes["bot_sensitive_content"] = sensitive_content
    result: dict[str, Any] = {
        "content": text, "message_type": "outgoing", "private": False,
        "content_attributes": attributes,
    }
    if choices:
        attributes["items"] = [{"title": c.label, "value": c.id} for c in choices]
        result["content_type"] = "input_select"
    return result


def _connector_receipt(message: dict[str, Any], message_id: int) -> bool:
    attributes = message.get("content_attributes")
    receipt = attributes.get("connector_delivery_receipt") if isinstance(attributes, dict) else None
    return (
        message.get("private") is True
        and message.get("message_type") in (1, "outgoing")
        and isinstance(receipt, dict)
        and type(receipt.get("message_id")) is int
        and receipt.get("message_id") == message_id
        and receipt.get("provider") in ("vk", "whatsapp")
        and isinstance(receipt.get("external_ids"), list)
        and bool(receipt["external_ids"])
        and all(isinstance(value, str) and value for value in receipt["external_ids"])
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


def _message_id(payload: Any) -> int | None:
    if isinstance(payload, dict) and type(payload.get("id")) is int:
        return payload["id"]
    return None

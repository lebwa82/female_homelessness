from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.chatwoot.client import AiohttpChatwootTransport, ChatwootClient
from app.domain import Choice


@dataclass
class RecordingTransport:
    responses: dict[tuple[str, str], Any] = field(default_factory=dict)
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = field(default_factory=list)

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, path, token, payload))
        return self.responses.get((method, path), {})


def client(transport: RecordingTransport) -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chat.example.test",
        account_id=12,
        read_token="read-token",
        bot_token="bot-token",
        transport=transport,
    )


def test_chatwoot_http_transport_disables_content_encoding_for_the_pinned_release() -> None:
    assert AiohttpChatwootTransport.request_headers("bot-token") == {
        "api_access_token": "bot-token",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }


@pytest.mark.asyncio
async def test_reads_conversation_and_messages_with_read_identity() -> None:
    transport = RecordingTransport(
        responses={
            ("GET", "/api/v1/accounts/12/conversations/23"): {"id": 23},
            ("GET", "/api/v1/accounts/12/conversations/23/messages"): {"payload": [{"id": 4}]},
        }
    )

    assert await client(transport).get_conversation(23) == {"id": 23}
    assert await client(transport).get_messages(23) == ({"id": 4},)
    assert [call[2] for call in transport.calls] == ["read-token", "read-token"]


@pytest.mark.asyncio
async def test_sends_telegram_input_select_and_persistent_turn_key() -> None:
    transport = RecordingTransport()

    await client(transport).send_reply(
        23,
        text="Choose one",
        choices=(Choice(id="option-a", label="Option A"), Choice(id="human", label="Talk to a person")),
        turn_key="message:41",
    )

    assert transport.calls == [
        (
            "POST",
            "/api/v1/accounts/12/conversations/23/messages",
            "bot-token",
            {
                "content": "Choose one",
                "message_type": "outgoing",
                "private": False,
                "content_type": "input_select",
                "content_attributes": {
                    "bot_turn_key": "message:41",
                    "items": [
                        {"title": "Option A", "value": "option-a"},
                        {"title": "Talk to a person", "value": "human"},
                    ],
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_mutations_use_agent_bot_identity() -> None:
    transport = RecordingTransport()
    api = client(transport)

    await api.set_custom_attributes(23, {"reply_owner": "human"})
    await api.set_status(23, "open")
    await api.assign_team(23, 9)
    await api.add_private_note(23, "handoff recorded")

    assert [call[2] for call in transport.calls] == ["bot-token"] * 4
    assert [call[:2] for call in transport.calls] == [
        ("POST", "/api/v1/accounts/12/conversations/23/custom_attributes"),
        ("POST", "/api/v1/accounts/12/conversations/23/toggle_status"),
        ("POST", "/api/v1/accounts/12/conversations/23/assignments"),
        ("POST", "/api/v1/accounts/12/conversations/23/messages"),
    ]


@pytest.mark.asyncio
async def test_detects_previously_sent_turn_key_before_retrying() -> None:
    transport = RecordingTransport(
        responses={
            ("GET", "/api/v1/accounts/12/conversations/23/messages"): {
                "payload": [
                    {"content_attributes": {"bot_turn_key": "message:41"}},
                    {"content_attributes": {}},
                ]
            }
        }
    )

    assert await client(transport).has_reply_for_turn(23, "message:41") is True

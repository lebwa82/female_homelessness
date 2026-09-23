from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.chatwoot.client import AiohttpChatwootTransport, BinaryAttachment, ChatwootClient
from app.domain import Choice


@dataclass
class RecordingTransport:
    responses: dict[tuple[str, str], Any] = field(default_factory=dict)
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = field(default_factory=list)
    multipart_calls: list[tuple[str, str, str, dict[str, str], BinaryAttachment]] = field(
        default_factory=list
    )

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, path, token, payload))
        return self.responses.get((method, path), {})

    async def request_multipart(
        self, method, path, token, fields, attachment
    ) -> Any:
        self.multipart_calls.append((method, path, token, fields, attachment))
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


async def test_catalog_members_and_return_use_correct_api_identities():
    transport = RecordingTransport(
        responses={
            ("GET", "/api/v1/accounts/12/teams"): [{"id": 9, "name": "Duty"}],
            ("GET", "/api/v1/accounts/12/teams/9/team_members"): [{"id": 4}, {"id": 5}],
        }
    )
    api = client(transport)
    assert (await api.get_teams())[0]["id"] == 9
    assert await api.get_team_members(9) == (4, 5)
    await api.unassign_human(23)
    assert [c[2] for c in transport.calls] == ["read-token", "read-token", "bot-token"]
    assert transport.calls[-1][3] == {"assignee_id": None}


@pytest.mark.asyncio
async def test_sends_telegram_input_select_and_persistent_turn_key() -> None:
    transport = RecordingTransport()

    await client(transport).send_reply(
        23,
        text="Choose one",
        choices=(
            Choice(id="option-a", label="Option A"),
            Choice(id="human", label="Talk to a person"),
        ),
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
    assert transport.calls[0][3] == {
        "custom_attributes": {"reply_owner": "human"},
        "merge": True,
    }
    assert transport.calls[2][3] == {"team_id": 9}
    assert [call[:2] for call in transport.calls] == [
        ("POST", "/api/v1/accounts/12/conversations/23/custom_attributes"),
        ("POST", "/api/v1/accounts/12/conversations/23/toggle_status"),
        ("POST", "/api/v1/accounts/12/conversations/23/assignments"),
        ("POST", "/api/v1/accounts/12/conversations/23/messages"),
    ]


@pytest.mark.asyncio
async def test_certificate_reply_is_marked_as_sensitive_chatwoot_content() -> None:
    transport = RecordingTransport()

    await client(transport).send_reply(
        23,
        text="Code: TEST-CODE",
        choices=(),
        turn_key="message:42",
        sensitive_content="certificate",
    )

    payload = transport.calls[0][3]
    assert payload is not None
    assert payload["content_attributes"] == {
        "bot_turn_key": "message:42",
        "bot_sensitive_content": "certificate",
    }


@pytest.mark.asyncio
async def test_pdf_certificate_uses_private_multipart_attachment_and_returns_message_id() -> None:
    path = "/api/v1/accounts/12/conversations/23/messages"
    transport = RecordingTransport(responses={("POST", path): {"id": 91}})

    message_id = await client(transport).send_reply(
        23,
        text="Certificate details",
        choices=(),
        turn_key="message:42:certificate",
        sensitive_content="certificate",
        attachment=BinaryAttachment(
            filename="certificate.pdf", content_type="application/pdf", data=b"%PDF-test"
        ),
    )

    assert message_id == 91
    assert transport.calls == []
    _, _, token, fields, attachment = transport.multipart_calls[0]
    assert token == "bot-token"
    assert attachment.filename == "certificate.pdf"
    assert "bot_sensitive_content" in fields["content_attributes"]


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


@pytest.mark.asyncio
async def test_private_notification_retry_uses_persisted_event_key() -> None:
    transport = RecordingTransport(
        responses={("GET", "/api/v1/accounts/12/conversations/23/messages"): {"payload": []}}
    )
    api = client(transport)
    await api.add_private_note(23, "notification", event_key="handoff:41")
    post = transport.calls[-1]
    assert post[2] == "bot-token"
    assert post[3]["private"] is True
    assert post[3]["content_attributes"] == {"bot_event_key": "handoff:41"}
    transport.responses[("GET", "/api/v1/accounts/12/conversations/23/messages")] = {
        "payload": [{"content_attributes": {"bot_event_key": "handoff:41"}}]
    }
    await api.add_private_note(23, "notification", event_key="handoff:41")
    assert len([call for call in transport.calls if call[0] == "POST"]) == 1

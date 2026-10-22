"""Neutral contracts for the native self-created-event optimization."""

import json

import pytest

from app.chatwoot.app import AgentBotWebhook
from app.chatwoot.contracts import (
    parse_conversation_changed,
    parse_message_created,
    parse_message_delivery_changed,
    parse_staff_message,
)


def self_event(private: bool, status: str) -> dict:
    return {
        "event": "message_created",
        "id": 11,
        "message_type": "outgoing",
        "private": private,
        "status": status,
        "sender": {"id": 7, "type": "agent_bot"},
        "conversation": {"id": 101},
        "inbox": {"id": 55},
        "content": "synthetic",
        "content_attributes": (
            {"bot_event_key": "scenario:" + "a" * 24}
            if private
            else {"bot_turn_key": "message:10"}
        ),
    }


class ForbiddenWork:
    async def enqueue(self, _event):
        pytest.fail("ignored native self event reached durable enqueue")

    async def process(self, _event):
        pytest.fail("ignored native self event reached the service")


class Request:
    def __init__(self, payload):
        self.headers = {}
        self.body = json.dumps(payload).encode()

    async def read(self):
        return self.body


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("status", ["sent", "delivered", "read", "failed"])
async def test_self_created_agent_bot_event_is_ignored_before_enqueue_or_processing(private, status):
    payload = self_event(private, status)
    parsers = (
        parse_message_created,
        parse_staff_message,
        parse_message_delivery_changed,
        parse_conversation_changed,
    )
    assert all(parser(payload) is None for parser in parsers)
    work = ForbiddenWork()
    webhook = AgentBotWebhook(work, route_secret="synthetic", event_queue=work)
    response = await webhook.handle(Request(payload))
    assert response.status == 204
    assert not webhook._tasks and not webhook._deliveries and not webhook._inflight


def test_user_sender_and_delivery_updates_must_not_be_classified_as_ignored_self_creation():
    payload = self_event(False, "sent")
    payload["sender"]["type"] = "user"
    assert parse_staff_message(payload) is not None
    payload["sender"]["type"] = "agent_bot"
    payload["event"] = "message_updated"
    payload["status"] = "failed"
    assert parse_message_delivery_changed(payload) is not None

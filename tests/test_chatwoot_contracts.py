from __future__ import annotations

import hashlib
import hmac
from time import time

import pytest

from app.chatwoot.contracts import IncomingChatwootMessage, parse_message_created
from app.chatwoot.webhook import InvalidWebhookSignature, verify_webhook_signature


def _message_event(*, message_type: str = "incoming") -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 41,
        "content": "test input",
        "message_type": message_type,
        "private": False,
        "conversation": {"id": 23, "status": "pending", "custom_attributes": {}},
        "sender": {"id": 7, "identifier": "telegram-user"},
        "inbox": {"id": 3, "channel_type": "Channel::Telegram"},
    }


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_parses_only_public_incoming_message_created_event() -> None:
    parsed = parse_message_created(_message_event())

    assert parsed == IncomingChatwootMessage(
        message_id=41,
        conversation_id=23,
        contact_id=7,
        inbox_id=3,
        content="test input",
    )


@pytest.mark.parametrize(
    "event",
    (
        _message_event(message_type="outgoing"),
        {**_message_event(), "private": True},
        {**_message_event(), "event": "conversation_updated"},
    ),
)
def test_ignores_non_user_events(event: dict[str, object]) -> None:
    assert parse_message_created(event) is None


def test_accepts_current_constant_time_hmac_signature() -> None:
    secret = "test-webhook-secret"
    timestamp = str(int(time()))
    body = b'{"event":"message_created"}'

    verify_webhook_signature(
        raw_body=body,
        timestamp=timestamp,
        received_signature=_signature(secret, timestamp, body),
        secret=secret,
        now=float(timestamp),
    )


def test_rejects_tampered_or_stale_webhook() -> None:
    secret = "test-webhook-secret"
    timestamp = "100"
    body = b'{"event":"message_created"}'

    with pytest.raises(InvalidWebhookSignature):
        verify_webhook_signature(
            raw_body=body,
            timestamp=timestamp,
            received_signature="sha256=not-a-real-signature",
            secret=secret,
            now=100.0,
        )

    with pytest.raises(InvalidWebhookSignature):
        verify_webhook_signature(
            raw_body=body,
            timestamp=timestamp,
            received_signature=_signature(secret, timestamp, body),
            secret=secret,
            now=401.0,
        )

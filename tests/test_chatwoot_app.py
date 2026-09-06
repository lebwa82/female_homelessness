from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from time import time
from typing import Any

import pytest

from app.chatwoot.app import AgentBotWebhook


@dataclass
class FakeRequest:
    body: bytes
    headers: dict[str, str]

    async def read(self) -> bytes:
        return self.body


@dataclass
class RecordingService:
    events: list[object] = field(default_factory=list)

    async def process(self, event: object) -> bool:
        self.events.append(event)
        return True


def _signed_request(payload: dict[str, Any], *, secret: str, delivery: str = "delivery-1") -> FakeRequest:
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time()))
    signature = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return FakeRequest(
        body=body,
        headers={
            "X-Chatwoot-Signature": signature,
            "X-Chatwoot-Timestamp": timestamp,
            "X-Chatwoot-Delivery": delivery,
        },
    )


def _payload() -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 41,
        "content": "test input",
        "message_type": "incoming",
        "private": False,
        "conversation": {"id": 23},
        "sender": {"id": 7},
        "inbox": {"id": 3},
    }


@pytest.mark.asyncio
async def test_signed_event_is_acknowledged_and_processed_once() -> None:
    service = RecordingService()
    webhook = AgentBotWebhook(
        service, route_secret="test-route-secret", signature_secret="test-hmac-secret"
    )
    request = _signed_request(_payload(), secret="test-hmac-secret")

    first = await webhook.handle(request)
    second = await webhook.handle(request)
    await asyncio.sleep(0)

    assert first.status == 204
    assert second.status == 204
    assert len(service.events) == 1


@pytest.mark.asyncio
async def test_bad_signature_is_rejected_without_starting_work() -> None:
    service = RecordingService()
    webhook = AgentBotWebhook(
        service, route_secret="test-route-secret", signature_secret="test-hmac-secret"
    )
    request = _signed_request(_payload(), secret="test-hmac-secret")
    request.headers["X-Chatwoot-Signature"] = "sha256=invalid"

    response = await webhook.handle(request)
    await asyncio.sleep(0)

    assert response.status == 401
    assert service.events == []


@pytest.mark.asyncio
async def test_legacy_unsigned_delivery_uses_message_identity_for_deduplication() -> None:
    service = RecordingService()
    webhook = AgentBotWebhook(service, route_secret="test-route-secret")
    raw_body = json.dumps(_payload(), separators=(",", ":")).encode()
    request = FakeRequest(body=raw_body, headers={})

    first = await webhook.handle(request)
    second = await webhook.handle(request)
    await asyncio.sleep(0)

    assert first.status == 204
    assert second.status == 204
    assert len(service.events) == 1


@pytest.mark.asyncio
async def test_partial_signature_headers_are_rejected() -> None:
    service = RecordingService()
    webhook = AgentBotWebhook(service, route_secret="test-route-secret")
    request = FakeRequest(
        body=json.dumps(_payload(), separators=(",", ":")).encode(),
        headers={"X-Chatwoot-Signature": "sha256=not-a-real-signature"},
    )

    response = await webhook.handle(request)
    await asyncio.sleep(0)

    assert response.status == 401
    assert service.events == []


@pytest.mark.asyncio
async def test_signed_mode_rejects_unsigned_legacy_delivery() -> None:
    service = RecordingService()
    webhook = AgentBotWebhook(
        service, route_secret="test-route-secret", signature_secret="test-hmac-secret"
    )
    request = FakeRequest(body=json.dumps(_payload(), separators=(",", ":")).encode(), headers={})

    response = await webhook.handle(request)
    await asyncio.sleep(0)

    assert response.status == 401
    assert service.events == []


@pytest.mark.asyncio
async def test_non_message_event_is_safely_ignored() -> None:
    service = RecordingService()
    webhook = AgentBotWebhook(
        service, route_secret="test-route-secret", signature_secret="test-hmac-secret"
    )
    request = _signed_request({"event": "conversation_updated"}, secret="test-hmac-secret")

    response = await webhook.handle(request)
    await asyncio.sleep(0)

    assert response.status == 204
    assert service.events == []

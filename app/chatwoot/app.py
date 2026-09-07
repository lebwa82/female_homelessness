"""Small HTTP surface for Chatwoot Agent Bot deliveries."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from aiohttp import web

from app.chatwoot.contracts import parse_conversation_changed, parse_message_created
from app.chatwoot.webhook import InvalidWebhookSignature, verify_webhook_signature

logger = logging.getLogger(__name__)


class EventProcessor(Protocol):
    async def process(self, event: object) -> bool: ...


class AgentBotWebhook:
    """Authenticate, minimally parse, acknowledge, then process a delivery."""

    def __init__(
        self,
        service: EventProcessor,
        *,
        route_secret: str,
        signature_secret: str = "",
    ) -> None:
        self._service = service
        self._route_secret = route_secret
        self._signature_secret = signature_secret
        self._deliveries: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, request: Any) -> web.Response:
        raw_body = await request.read()
        timestamp = _header(request.headers, "X-Chatwoot-Timestamp")
        signature = _header(request.headers, "X-Chatwoot-Signature")
        delivery_id = _header(request.headers, "X-Chatwoot-Delivery")
        signed_headers = (timestamp, signature, delivery_id)
        if any(signed_headers) and not all(signed_headers):
            return web.Response(status=401)
        if all(signed_headers):
            if not self._signature_secret:
                return web.Response(status=401)
            try:
                verify_webhook_signature(
                    raw_body=raw_body,
                    timestamp=timestamp,
                    received_signature=signature,
                    secret=self._signature_secret,
                )
            except InvalidWebhookSignature:
                return web.Response(status=401)
        elif self._signature_secret:
            return web.Response(status=401)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return web.Response(status=204)
        event = parse_message_created(payload) or parse_conversation_changed(payload)
        if event is None:
            return web.Response(status=204)

        # Legacy installations without signed deliveries use message identity.
        # Assignment changes have no message id and must be re-read each time.
        if not delivery_id and hasattr(event, "message_id"):
            delivery_id = f"legacy:{event.conversation_id}:{event.message_id}"
        if delivery_id and delivery_id in self._deliveries:
            return web.Response(status=204)

        if delivery_id:
            self._deliveries.add(delivery_id)
        task = asyncio.create_task(self._process(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return web.Response(status=204)

    async def _process(self, event: object) -> None:
        for attempt in range(3):
            try:
                await self._service.process(event)
                return
            except Exception as error:  # noqa: BLE001 - never log user payload or provider details
                logger.warning("chatwoot agent delivery failed: %s", type(error).__name__)
                if attempt < 2:
                    await asyncio.sleep(3 * (attempt + 1))


def create_application(
    service: EventProcessor,
    *,
    route_secret: str,
    signature_secret: str = "",
) -> web.Application:
    webhook = AgentBotWebhook(
        service,
        route_secret=route_secret,
        signature_secret=signature_secret,
    )
    app = web.Application()
    app.router.add_get("/healthz", _health)
    app.router.add_post(f"/webhooks/chatwoot/agent/{route_secret}", webhook.handle)
    return app


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _header(headers: Any, name: str) -> str:
    value = headers.get(name)
    if isinstance(value, str):
        return value
    normalized = name.lower()
    for key, candidate in headers.items():
        if str(key).lower() == normalized and isinstance(candidate, str):
            return candidate
    return ""

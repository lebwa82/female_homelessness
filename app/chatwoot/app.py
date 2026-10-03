"""Small HTTP surface for Chatwoot Agent Bot deliveries."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import Any, Protocol

from aiohttp import web

from app.chatwoot.contracts import (
    parse_conversation_changed,
    parse_message_created,
    parse_message_delivery_changed,
    parse_staff_message,
)
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
        event_queue=None,
    ) -> None:
        self._service = service
        self._route_secret = route_secret
        self._signature_secret = signature_secret
        self._event_queue = event_queue
        self._deliveries: OrderedDict[str, None] = OrderedDict()
        self._inflight: set[str] = set()
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
        event = (
            parse_message_created(payload) or parse_staff_message(payload)
            or parse_message_delivery_changed(payload)
            or parse_conversation_changed(payload)
        )
        if event is None:
            return web.Response(status=204)

        if self._event_queue is not None:
            try:
                await self._event_queue.enqueue(event)
            except Exception as error:  # noqa: BLE001 - sender must retry; never acknowledge lost work
                logger.error("chatwoot enqueue failed: type=%s", type(error).__name__)
                return web.Response(status=503)
            return web.Response(status=204)

        # Legacy installations without signed deliveries use message identity.
        # Assignment changes have no message id and must be re-read each time.
        if not delivery_id and hasattr(event, "message_id"):
            delivery_id = (f"legacy:{type(event).__name__}:{event.conversation_id}:"
                           f"{event.message_id}:{getattr(event, 'status', '')}")
        if delivery_id and (delivery_id in self._deliveries or delivery_id in self._inflight):
            return web.Response(status=204)

        if delivery_id:
            self._inflight.add(delivery_id)
        task = asyncio.create_task(self._process(event, delivery_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return web.Response(status=204)

    async def _process(self, event: object, delivery_id: str = "") -> None:
        try:
            for attempt in range(3):
                try:
                    await self._service.process(event)
                    if delivery_id:
                        self._deliveries[delivery_id] = None
                        if len(self._deliveries) > 2048:
                            self._deliveries.popitem(last=False)
                    return
                except Exception as error:  # noqa: BLE001 - no user payload or provider details
                    logger.warning(
                        "chatwoot delivery failed: event=%s conversation=%s message=%s attempt=%s type=%s",
                        type(event).__name__, getattr(event, "conversation_id", None),
                        getattr(event, "message_id", None), attempt + 1, type(error).__name__,
                    )
                    if attempt < 2:
                        await asyncio.sleep(3 * (attempt + 1))
            logger.error("chatwoot delivery exhausted: conversation=%s message=%s",
                         getattr(event, "conversation_id", None), getattr(event, "message_id", None))
        finally:
            # Failed/cancelled deliveries must be eligible for redelivery.
            self._inflight.discard(delivery_id)

    async def shutdown(self, _app: web.Application) -> None:
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=25)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


def create_application(
    service: EventProcessor,
    *,
    route_secret: str,
    signature_secret: str = "",
    event_queue=None,
) -> web.Application:
    webhook = AgentBotWebhook(
        service,
        route_secret=route_secret,
        signature_secret=signature_secret,
        event_queue=event_queue,
    )
    app = web.Application()
    async def health(request):
        if event_queue is not None and not await event_queue.healthy():
            return web.json_response({"status": "degraded"}, status=503)
        return await _health(request)

    app.router.add_get("/healthz", health)
    app.router.add_post(f"/webhooks/chatwoot/agent/{route_secret}", webhook.handle)
    app.on_shutdown.append(webhook.shutdown)
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

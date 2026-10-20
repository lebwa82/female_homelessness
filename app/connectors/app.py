"""Only the connector webhooks are exposed by this standalone HTTP application."""

import hmac
import json
import logging

from aiohttp import web

from app.connectors.chatwoot import outgoing_event

logger = logging.getLogger(__name__)


def create_application(settings, provider, queue):
    async def health(_request):
        try:
            healthy = await queue.healthy()
        except Exception:  # noqa: BLE001 - keep credentials out of health output
            healthy = False
        return web.json_response({"status": "ok" if healthy else "degraded"},
                                 status=200 if healthy else 503)

    async def enqueue(events):
        try:
            for event in events:
                await queue.enqueue(event)
        except Exception as error:  # noqa: BLE001 - sender retries an unacknowledged batch
            logger.warning("connector enqueue failed: type=%s", type(error).__name__)
            raise web.HTTPServiceUnavailable() from None

    async def inbound(request):
        raw = await request.read()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest()
        if not provider.authenticate(raw, request.headers, payload):
            raise web.HTTPUnauthorized()
        if settings.provider == "vk" and payload.get("type") == "confirmation":
            return web.Response(text=settings.vk_confirmation.get_secret_value())
        try:
            events = [{"kind": "incoming", **event} for event in provider.incoming(payload)]
            if settings.provider == "whatsapp":
                events.extend(provider.statuses(payload))
        except (TypeError, ValueError, AttributeError, KeyError):
            raise web.HTTPBadRequest() from None
        await enqueue(events)
        return web.Response(text="ok" if settings.provider == "vk" else "EVENT_RECEIVED")

    async def verify_whatsapp(request):
        query = request.query
        supplied = query.get("hub.verify_token", "")
        if (query.get("hub.mode") != "subscribe" or not hmac.compare_digest(
                supplied.encode(), settings.whatsapp_verify_token.get_secret_value().encode())):
            raise web.HTTPForbidden()
        challenge = query.get("hub.challenge", "")
        if not challenge.isascii() or not challenge.isdigit() or len(challenge) > 100:
            raise web.HTTPBadRequest()
        return web.Response(text=challenge)

    async def outbound(request):
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        try:
            event = outgoing_event(payload, settings)
        except (TypeError, AttributeError):
            raise web.HTTPBadRequest() from None
        if event:
            await enqueue([event])
        return web.Response(status=204)

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/healthz", health)
    app.router.add_post(f"/webhooks/{settings.provider}", inbound)
    if settings.provider == "whatsapp":
        app.router.add_get("/webhooks/whatsapp", verify_whatsapp)
    secret = settings.callback_secret.get_secret_value()
    app.router.add_post(f"/webhooks/chatwoot/{secret}", outbound)
    return app

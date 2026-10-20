"""python -m app.connectors; one provider, one process, one private Redis."""

import asyncio
import logging
import os
import signal
from contextlib import suppress

import aiohttp
from aiohttp import web
from pydantic import ValidationError
from redis.asyncio import Redis

from app.connectors.app import create_application
from app.connectors.chatwoot import Chatwoot
from app.connectors.config import ConnectorSettings
from app.connectors.http import Http
from app.connectors.queue import ConnectorQueue
from app.connectors.service import ConnectorService


def load_provider(settings, http, redis):
    # Import only the selected adapter. A provider's optional code never enters
    # another provider's runtime. Telegram never imports this package.
    if settings.provider == "vk":
        from app.connectors.vk import VK
        return VK(settings, http)
    from app.connectors.whatsapp import WhatsApp
    return WhatsApp(settings, http, redis)


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        settings = ConnectorSettings()
    except ValidationError as error:
        # Pydantic's default error includes input_value, which can be a secret.
        fields = sorted({".".join(str(p) for p in item["loc"]) or "configuration"
                         for item in error.errors(include_input=False)})
        raise SystemExit("invalid connector configuration: " + ", ".join(fields)) from None

    async def application():
        redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True,
                               socket_connect_timeout=5, socket_timeout=10)
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=True)
        http = Http(session, max_bytes=settings.max_attachment_bytes)
        chatwoot = Chatwoot(settings, http)
        provider = load_provider(settings, http, redis)
        service = ConnectorService(settings, redis, chatwoot, provider)
        queue = ConnectorQueue(redis, service, namespace=settings.scope)
        app = create_application(settings, provider, queue)

        async def lifecycle(_app):
            supervisor = None

            async def stop_on_lease_loss():
                await queue.lease_lost.wait()
                # An unhealthy Docker check alone does not restart a process.
                # Exit gracefully so this connector's restart policy can recover.
                os.kill(os.getpid(), signal.SIGTERM)

            try:
                await chatwoot.check_inbox()
                await queue.start()
                supervisor = asyncio.create_task(stop_on_lease_loss())
                yield
            finally:
                if supervisor:
                    supervisor.cancel()
                    with suppress(asyncio.CancelledError):
                        await supervisor
                try:
                    await queue.close()
                finally:
                    await session.close()
                    await redis.aclose()

        app.cleanup_ctx.append(lifecycle)
        return app

    # Callback URL and Meta verify token must not appear in access logs.
    web.run_app(application(), host=settings.listen_host, port=settings.listen_port,
                access_log=None, print=None)


if __name__ == "__main__":
    main()

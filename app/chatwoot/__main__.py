"""Run the stateless Chatwoot Agent Bot HTTP service."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from aiohttp import web
from redis.asyncio import Redis

from app.certificate_documents import S3CertificateObjectStore
from app.chatwoot.app import create_application
from app.chatwoot.certificates import CertificateInventory, database_url
from app.chatwoot.client import ChatwootClient
from app.chatwoot.event_queue import DurableEventQueue
from app.chatwoot.followups import run as run_followups
from app.chatwoot.service import ChatwootAgentService
from app.config import settings


def main() -> None:
    if error := settings.chatwoot_configuration_error():
        raise SystemExit(error)
    if not settings.certificate_database_password:
        raise SystemExit("missing CERTIFICATE_DATABASE_PASSWORD")
    if not settings.certificate_identity_key:
        raise SystemExit("missing CERTIFICATE_IDENTITY_KEY")
    if error := settings.certificate_s3_configuration_error():
        raise SystemExit(error)
    inventory = CertificateInventory(
        database_url(settings.certificate_database_password),
        identity_key=settings.certificate_identity_key,
        account_id=settings.chatwoot_account_id,
    )
    certificate_store = S3CertificateObjectStore(
        bucket=settings.certificate_s3_bucket,
        endpoint_url=settings.certificate_s3_endpoint,
        region=settings.certificate_s3_region,
        access_key_id=settings.certificate_s3_access_key_id,
        secret_access_key=settings.certificate_s3_secret_access_key,
    )
    client = ChatwootClient(
        base_url=settings.chatwoot_base_url,
        account_id=settings.chatwoot_account_id,
        read_token=settings.chatwoot_read_token,
        bot_token=settings.chatwoot_bot_token,
    )
    service = ChatwootAgentService(
        client,
        duty_team_id=settings.chatwoot_duty_team_id,
        certificate_claim=inventory.claim,
        certificate_store=certificate_store,
        certificate_mark_submitted=inventory.mark_submitted,
        certificate_mark_failed=inventory.mark_failed,
        certificate_mark_delivered=inventory.mark_delivered,
        certificate_mark_delivery_failed=inventory.mark_delivery_failed,
    )
    redis = Redis.from_url(settings.telegram_ingress_redis_url, decode_responses=True,
                           socket_timeout=5, socket_connect_timeout=5)
    event_queue = DurableEventQueue(redis, service,
                                   namespace=f"women-help:chatwoot-events:{settings.chatwoot_account_id}")
    application = create_application(
        service,
        route_secret=settings.chatwoot_webhook_secret,
        signature_secret=settings.chatwoot_webhook_hmac_secret,
        event_queue=event_queue,
    )

    async def lifecycle(_app: web.Application):
        await inventory.initialize()
        await event_queue.start()
        worker = asyncio.create_task(run_followups(client, service))
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            await event_queue.close()
            await redis.aclose()
            await inventory.close()

    application.cleanup_ctx.append(lifecycle)
    web.run_app(
        application,
        host=settings.chatwoot_listen_host,
        port=settings.chatwoot_listen_port,
        # The webhook's URL includes its route credential. Standard aiohttp
        # access logging would copy that credential into the container journal.
        access_log=None,
    )


if __name__ == "__main__":
    main()

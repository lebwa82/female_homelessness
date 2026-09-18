"""Run the stateless Chatwoot Agent Bot HTTP service."""

from __future__ import annotations

from aiohttp import web

from app.chatwoot.app import create_application
from app.chatwoot.certificates import CertificateInventory, database_url
from app.chatwoot.client import ChatwootClient
from app.chatwoot.service import ChatwootAgentService
from app.config import settings


def main() -> None:
    if error := settings.chatwoot_configuration_error():
        raise SystemExit(error)
    if not settings.certificate_database_password:
        raise SystemExit("missing CERTIFICATE_DATABASE_PASSWORD")
    if not settings.certificate_identity_key:
        raise SystemExit("missing CERTIFICATE_IDENTITY_KEY")
    inventory = CertificateInventory(
        database_url(settings.certificate_database_password),
        identity_key=settings.certificate_identity_key,
        account_id=settings.chatwoot_account_id,
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
    )
    application = create_application(
        service,
        route_secret=settings.chatwoot_webhook_secret,
        signature_secret=settings.chatwoot_webhook_hmac_secret,
    )

    async def on_startup(_app: web.Application) -> None:
        await inventory.initialize()

    async def on_cleanup(_app: web.Application) -> None:
        await inventory.close()

    application.on_startup.append(on_startup)
    application.on_cleanup.append(on_cleanup)
    web.run_app(
        application,
        host=settings.chatwoot_listen_host,
        port=settings.chatwoot_listen_port,
    )


if __name__ == "__main__":
    main()

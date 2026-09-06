"""Run the stateless Chatwoot Agent Bot HTTP service."""

from __future__ import annotations

from aiohttp import web

from app.chatwoot.app import create_application
from app.chatwoot.client import ChatwootClient
from app.chatwoot.service import ChatwootAgentService
from app.config import settings


def main() -> None:
    if error := settings.chatwoot_configuration_error():
        raise SystemExit(error)
    client = ChatwootClient(
        base_url=settings.chatwoot_base_url,
        account_id=settings.chatwoot_account_id,
        read_token=settings.chatwoot_read_token,
        bot_token=settings.chatwoot_bot_token,
    )
    service = ChatwootAgentService(
        client,
        duty_team_id=settings.chatwoot_duty_team_id,
    )
    web.run_app(
        create_application(
            service,
            route_secret=settings.chatwoot_webhook_secret,
            signature_secret=settings.chatwoot_webhook_hmac_secret,
        ),
        host=settings.chatwoot_listen_host,
        port=settings.chatwoot_listen_port,
    )


if __name__ == "__main__":
    main()

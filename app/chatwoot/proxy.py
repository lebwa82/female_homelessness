"""Internal HTTP CONNECT adapter for the existing Telegram SOCKS proxy.

No host port is published; Chatwoot keeps its native Telegram channel and TLS
stays end-to-end. Credentials are read from the environment, never argv/logs.
"""

from __future__ import annotations

import asyncio
from urllib.parse import unquote, urlsplit

import pproxy

from app.config import settings


def upstream_uri(value: str) -> str:
    parsed = urlsplit(value)
    schemes = {"socks5": "socks5", "socks4": "socks4", "http": "http", "https": "http+ssl"}
    if parsed.scheme not in schemes or not parsed.hostname or not parsed.port:
        raise ValueError("Unsupported Telegram proxy configuration")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    auth = ""
    if parsed.username is not None:
        auth = f"#{unquote(parsed.username)}:{unquote(parsed.password or '')}"
    return f"{schemes[parsed.scheme]}://{host}:{parsed.port}{auth}"


async def serve() -> None:
    value = settings.resolved_telegram_proxy_url
    if not value:
        raise ValueError("TELEGRAM_PROXY_URL is required for the internal adapter")
    server = pproxy.Server("http://0.0.0.0:3128")
    remote = pproxy.Connection(upstream_uri(value))
    handler = await server.start_server(
        {
            "rserver": [remote],
            "block": lambda host: host.lower().rstrip(".") != "api.telegram.org",
        }
    )
    async with handler:
        await handler.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - never expose proxy credentials in exceptions
        raise SystemExit(f"Telegram proxy adapter failed ({type(error).__name__})") from None

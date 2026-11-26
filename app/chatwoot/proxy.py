"""Internal HTTP CONNECT adapter for the existing Telegram SOCKS proxy.

No host port is published; Chatwoot keeps its native Telegram channel and TLS
stays end-to-end. Credentials are read from the environment, never argv/logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import unquote, urlsplit

import pproxy

from app.config import settings

logger = logging.getLogger(__name__)

PROXY_SOCKETS = "/run/nevidimy-proxy"


class TelegramUpstream:
    """Bound/retry only opening a tunnel, before any Telegram request can pass."""

    def __init__(self, connection, reserve=None):
        self.connection = connection
        self.reserve = reserve

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def open_connection(self, host, port, local_addr, lbind):
        routes = (
            [(self.connection, 3, "primary"), (self.reserve, 5, "reserve")]
            if self.reserve is not None
            else [(self.connection, 5, "primary")] * 3
        )
        for attempt, (connection, timeout, route) in enumerate(routes, 1):
            writer = None
            try:
                async with asyncio.timeout(timeout):
                    reader, writer = await connection.open_connection(
                        host, port, local_addr, lbind, timeout=timeout
                    )
                    return await connection.prepare_connection(reader, writer, host, port)
            except (TimeoutError, OSError, EOFError, AssertionError) as error:
                if writer is not None:
                    writer.close()
                logger.warning("Telegram proxy tunnel failed route=%s kind=%s attempt=%s",
                               route, type(error).__name__, attempt)
                if attempt == len(routes):
                    raise
                if self.reserve is None:
                    await asyncio.sleep(0.2 * attempt)
            except BaseException:
                if writer is not None:
                    writer.close()
                raise

    async def prepare_connection(self, reader, writer, host, port):
        # pproxy acknowledges CONNECT only after this returns. SOCKS negotiation
        # was already completed above; never retry once encrypted traffic flows.
        return reader, writer


def upstream_uri(
    value: str, *, ssh_tunnel: bool = False, socket_path: str = f"{PROXY_SOCKETS}/socks.sock"
) -> str:
    parsed = urlsplit(value)
    schemes = {"socks5": "socks5", "socks4": "socks4", "http": "http", "https": "http+ssl"}
    if parsed.scheme not in schemes or not parsed.hostname or not parsed.port:
        raise ValueError("Unsupported Telegram proxy configuration")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    auth = ""
    if parsed.username is not None:
        auth = f"#{unquote(parsed.username)}:{unquote(parsed.password or '')}"
    if ssh_tunnel:
        if parsed.scheme != "socks5":
            raise ValueError("SSH tunnel requires the existing SOCKS5 upstream")
        return f"socks5://{socket_path}{auth}"
    return f"{schemes[parsed.scheme]}://{host}:{parsed.port}{auth}"


async def serve() -> None:
    value = settings.resolved_telegram_proxy_url()
    if not value:
        raise ValueError("TELEGRAM_PROXY_URL is required for the internal adapter")
    reserve_url = os.environ.get("TELEGRAM_PROXY_RESERVE_URL", "").strip()
    reserve = None
    if reserve_url:
        if not settings.telegram_proxy_ssh_tunnel:
            raise ValueError("Telegram reserve requires SSH tunnels")
        reserve = pproxy.Connection(upstream_uri(
            reserve_url, ssh_tunnel=True, socket_path=f"{PROXY_SOCKETS}/reserve-socks.sock"
        ))
    server = pproxy.Server("http://0.0.0.0:3128")
    remote = TelegramUpstream(pproxy.Connection(upstream_uri(
        value, ssh_tunnel=settings.telegram_proxy_ssh_tunnel
    )), reserve)
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

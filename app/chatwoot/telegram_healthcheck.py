"""Lightweight polling liveness probe; never imports the Telegram SDK or polls."""

from __future__ import annotations

import asyncio
import time

from redis.asyncio import Redis

LEASE_SECONDS = 120


async def healthy() -> bool:
    from app.config import settings

    bot_id = int(settings.telegram_bot_token.split(":", 1)[0])
    async with Redis.from_url(settings.telegram_ingress_redis_url,
                              socket_timeout=5, socket_connect_timeout=5) as redis:
        stamp = await redis.get(f"women-help:telegram-ingress:{bot_id}:last_poll")
        return stamp is not None and 0 <= time.time() - int(stamp) < LEASE_SECONDS


if __name__ == "__main__":
    try:
        raise SystemExit(0 if asyncio.run(healthy()) else 1)
    except Exception:  # noqa: BLE001 - probe output must not contain credentials
        raise SystemExit(1) from None

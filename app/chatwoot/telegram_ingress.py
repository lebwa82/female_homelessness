"""Outbound-only Telegram ingress; Chatwoot still owns messages and replies.

Uses its native Telegram handler, not a second conversation implementation.
The Redis cursor advances only after Chatwoot has accepted the update into
Sidekiq. Delivery is at least once: an ambiguous HTTP failure after enqueueing
can cause a duplicate; the native Chatwoot endpoint has no idempotency contract.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from contextlib import suppress

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import Update
from aiogram.utils.serialization import deserialize_telegram_object_to_python
from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)
POLL_TIMEOUT = 30
LEASE_SECONDS = 120


class ChatwootUnavailable(RuntimeError):
    """Intentionally carries no URL, response body, token or message text."""


class Ingress:
    def __init__(self, bot: Bot, http: aiohttp.ClientSession, redis: Redis, base_url: str):
        self.bot, self.http, self.redis = bot, http, redis
        self.base_url = base_url.rstrip("/")
        self.prefix = f"women-help:telegram-ingress:{bot.id}"

    async def prepare(self) -> None:
        # Do not disable Telegram delivery before the local receiver is ready.
        async with self.http.get(
            f"{self.base_url}/", allow_redirects=False
        ) as response:
            if response.status not in {200, 302}:
                raise ChatwootUnavailable()
        await self.bot.get_me(request_timeout=15)
        await self.bot.delete_webhook(drop_pending_updates=False, request_timeout=15)
        logger.info("Polling prepared; queued Telegram updates preserved")

    async def forward(self, update: Update) -> None:
        # Explicit wrapper matches Webhooks::TelegramEventsJob, including callbacks.
        payload = deserialize_telegram_object_to_python(
            update.model_dump(mode="python", by_alias=True, exclude_none=True)
        )
        async with self.http.post(
            f"{self.base_url}/webhooks/telegram/{self.bot.token}",
            json={"telegram": payload},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ChatwootUnavailable()

    async def poll_once(self) -> int:
        saved = await self.redis.get(f"{self.prefix}:offset")
        offset = int(saved) if saved is not None else None
        updates = await self.bot.get_updates(
            offset=offset, limit=1, timeout=POLL_TIMEOUT,
            allowed_updates=["message", "edited_message", "callback_query"],
            request_timeout=POLL_TIMEOUT + 15,
        )
        for update in updates:
            if offset is not None and update.update_id < offset:
                continue
            started = time.monotonic()
            await self.forward(update)
            # A successful HTTP response means Rails has enqueued the native job.
            # Persist before requesting the next batch (Telegram's acknowledgement).
            await self.redis.set(f"{self.prefix}:offset", update.update_id + 1)
            logger.info("Update forwarded id=%s latency_ms=%s", update.update_id,
                        round((time.monotonic() - started) * 1000))
            if update.callback_query:
                try:
                    await self.bot.answer_callback_query(
                        update.callback_query.id, request_timeout=10
                    )
                except TelegramAPIError as error:
                    # An expired spinner must not replay an already stored click.
                    logger.warning("Callback acknowledgement failed kind=%s", type(error).__name__)
        await self.redis.set(f"{self.prefix}:last_poll", int(time.time()))
        return len(updates)

    async def poll_forever(self) -> None:
        delay = 1
        while True:
            try:
                await self.poll_once()
                delay = 1
            except TelegramConflictError:
                # Editing a native Telegram channel in Chatwoot can register its
                # webhook again. Reconcile it only while owning the ingress lease.
                info = await self.bot.get_webhook_info(request_timeout=15)
                if info.url:
                    await self.bot.delete_webhook(drop_pending_updates=False, request_timeout=15)
                logger.warning("Telegram transport conflict; retrying with preserved cursor")
                await asyncio.sleep(10)
            except TelegramRetryAfter as error:
                logger.warning("Telegram rate limit; retrying")
                await asyncio.sleep(max(1, error.retry_after))
            except (TelegramNetworkError, TelegramServerError, aiohttp.ClientError,
                    TimeoutError, ChatwootUnavailable) as error:
                logger.warning("Ingress retry kind=%s delay_s=%s", type(error).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


async def keep_lease(lock) -> None:
    while True:
        await asyncio.sleep(20)
        # On Redis loss / lease loss TaskGroup cancels the polling request.
        await lock.extend(LEASE_SECONDS, replace_ttl=True)


async def run() -> None:
    if settings.telegram_update_transport != "polling":
        raise RuntimeError("Polling transport is not enabled")
    proxy = settings.resolved_telegram_proxy_url()
    if not proxy or not settings.chatwoot_base_url:
        raise RuntimeError("Polling requires a proxy and an internal Chatwoot URL")
    async with (
        Bot(settings.telegram_bot_token, session=AiohttpSession(proxy=proxy)) as bot,
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                              headers={"Accept-Encoding": "identity"}) as http,
        Redis.from_url(settings.telegram_ingress_redis_url, socket_timeout=5,
                       socket_connect_timeout=5) as redis,
    ):
        ingress = Ingress(bot, http, redis, settings.chatwoot_base_url)
        lock = redis.lock(f"{ingress.prefix}:lock", timeout=LEASE_SECONDS,
                          blocking_timeout=5, thread_local=False)
        async with lock, asyncio.TaskGroup() as group:
            group.create_task(keep_lease(lock))
            await ingress.prepare()
            await ingress.poll_forever()


async def healthy() -> bool:
    # Probe only Redis metadata, never consume Telegram updates in a health check.
    bot_id = int(settings.telegram_bot_token.split(":", 1)[0])
    async with Redis.from_url(settings.telegram_ingress_redis_url,
                              socket_timeout=5, socket_connect_timeout=5) as redis:
        stamp = await redis.get(f"women-help:telegram-ingress:{bot_id}:last_poll")
        return stamp is not None and 0 <= time.time() - int(stamp) < LEASE_SECONDS


async def main() -> None:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, task.cancel)
    with suppress(asyncio.CancelledError):
        await run()


def error_kinds(error: BaseException) -> str:
    """Expose TaskGroup failure classes without its secret-bearing traceback."""
    if isinstance(error, BaseExceptionGroup):
        return ",".join(sorted({error_kinds(child) for child in error.exceptions}))
    return type(error).__name__


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if "--check" in sys.argv:
            raise SystemExit(0 if asyncio.run(healthy()) else 1)
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - SDK errors can embed tokens and payloads
        logger.error("Telegram ingress stopped kind=%s", error_kinds(error))
        raise SystemExit(1) from None

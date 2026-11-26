"""Small restart-safe worker for the single-instance developer deployment."""

import asyncio
import logging
from datetime import UTC, datetime

from app.chatwoot.scenario_effects import daytime

logger = logging.getLogger(__name__)


class HumanReminderIndex:
    """Find due handoff prompts after hours without scanning every conversation."""

    def __init__(self, redis, *, account_id: int) -> None:
        self.redis = redis
        self.key = f"women-help:human-reminders:{account_id}"

    async def add(self, conversation_id: int, due: datetime) -> None:
        await self.redis.zadd(self.key, {str(conversation_id): due.timestamp()})

    async def discard(self, conversation_id: int) -> None:
        await self.redis.zrem(self.key, str(conversation_id))

    async def due(self, now: datetime) -> list[int]:
        return [int(value) for value in await self.redis.zrangebyscore(self.key, "-inf", now.timestamp())]


async def sweep(api, service, *, now: datetime | None = None,
                human_reminders: HumanReminderIndex | None = None) -> int:
    now = now or datetime.now(UTC)
    if daytime(now) > now:
        sent = 0
        if human_reminders is not None:
            for conversation_id in await human_reminders.due(now):
                try:
                    sent += await service.send_indexed_human_reminder(conversation_id)
                except Exception as error:  # noqa: BLE001 - retry on the next sweep
                    logger.warning("human reminder failed: %s", type(error).__name__)
        return sent
    page, sent = 1, 0
    seen = set()
    while conversations := await api.list_conversations(page):
        fresh = [c for c in conversations if c["id"] not in seen]
        if not fresh:
            break
        for conversation in fresh:
            seen.add(conversation["id"])
            try:
                jobs = (conversation.get("custom_attributes") or {}).get("scenario_followups") or {}
                if not any(job["state"] == "pending"
                           and datetime.fromisoformat(job["due_at"]) <= now
                           for job in jobs.values()):
                    continue
                # The list is only a cheap candidate filter. Sending still
                # rereads current ownership, cancellation and schedule under lock.
                sent += await service.send_due_followup(conversation["id"])
            except Exception as error:  # noqa: BLE001 - isolate conversations, no personal data
                logger.warning("scenario followup failed: %s", type(error).__name__)
        page += 1
    return sent


async def run(api, service, *, human_reminders: HumanReminderIndex) -> None:
    while True:
        try:
            await sweep(api, service, human_reminders=human_reminders)
        except Exception as error:  # noqa: BLE001 - retry transient network failures next sweep
            logger.warning("scenario followup sweep failed: %s", type(error).__name__)
        await asyncio.sleep(60)

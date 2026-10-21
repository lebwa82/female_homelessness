"""Small restart-safe worker for the single-instance developer deployment."""

import asyncio
import logging
from datetime import UTC, datetime

from app.chatwoot.scenario_effects import daytime

logger = logging.getLogger(__name__)


async def sweep(api, service, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    if daytime(now) > now:
        return 0
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


async def run(api, service) -> None:
    while True:
        try:
            await sweep(api, service)
        except Exception as error:  # noqa: BLE001 - retry transient network failures next sweep
            logger.warning("scenario followup sweep failed: %s", type(error).__name__)
        await asyncio.sleep(60)

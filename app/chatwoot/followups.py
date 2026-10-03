"""Small restart-safe worker for the single-instance developer deployment."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def sweep(api, service) -> int:
    page, sent = 1, 0
    seen = set()
    while conversations := await api.list_conversations(page):
        fresh = [c for c in conversations if c["id"] not in seen]
        if not fresh:
            break
        for conversation in fresh:
            seen.add(conversation["id"])
            if not (conversation.get("custom_attributes") or {}).get("scenario_followups"):
                continue
            try:
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

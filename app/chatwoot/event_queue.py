"""Redis Streams inbox: acknowledge webhooks only after durable enqueueing.

One deployed agent-bot process, eight ordered lanes. A conversation always uses
the same lane; failed work stays pending and is retried, including after restart.
Redis already belongs to the Chatwoot contour and has AOF persistence enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

from redis.exceptions import ResponseError

from app.chatwoot.contracts import (
    ConversationChanged,
    IncomingChatwootMessage,
    MessageDeliveryChanged,
    StaffMessage,
)

logger = logging.getLogger(__name__)
EVENTS = {kind.__name__: kind for kind in (
    IncomingChatwootMessage, ConversationChanged, StaffMessage, MessageDeliveryChanged,
)}


class DurableEventQueue:
    def __init__(self, redis, service, *, namespace: str, lanes: int = 8):
        self.redis, self.service = redis, service
        self.keys = tuple(f"{namespace}:{i}" for i in range(lanes))
        self.group, self.consumer = "agent", "single-instance"
        self.tasks: list[asyncio.Task] = []
        self.last_errors: dict[str, str] = {}

    async def enqueue(self, event: object) -> None:
        kind = type(event).__name__
        if kind not in EVENTS:
            raise ValueError("unsupported_event_type")
        key = self.keys[event.conversation_id % len(self.keys)]
        await self.redis.xadd(key, {
            "type": kind, "payload": json.dumps(asdict(event), ensure_ascii=False),
        })

    async def start(self) -> None:
        for key in self.keys:
            try:
                await self.redis.xgroup_create(key, self.group, id="0", mkstream=True)
            except ResponseError as error:
                if not str(error).startswith("BUSYGROUP"):
                    raise
        self.tasks = [asyncio.create_task(self._run(key)) for key in self.keys]

    async def consume_one(self, key: str, *, block_ms: int = 1000) -> bool:
        # Our fixed consumer name resumes unacknowledged work after process death.
        batches = await self.redis.xreadgroup(self.group, self.consumer, {key: "0"}, count=1)
        if not any(messages for _, messages in batches):
            batches = await self.redis.xreadgroup(self.group, self.consumer, {key: ">"},
                                                  count=1, block=block_ms)
        for _, messages in batches:
            for entry_id, fields in messages:
                event = EVENTS[fields["type"]](**json.loads(fields["payload"]))
                await self.service.process(event)
                async with self.redis.pipeline(transaction=True) as transaction:
                    transaction.xack(key, self.group, entry_id)
                    transaction.xdel(key, entry_id)
                    await transaction.execute()
                return True
        return False

    async def _run(self, key: str) -> None:
        failures = 0
        while True:
            try:
                await self.consume_one(key)
                failures = 0
                self.last_errors.pop(key, None)
            except Exception as error:  # noqa: BLE001 - retain pending event, never log its payload
                failures += 1
                self.last_errors[key] = type(error).__name__
                logger.error("chatwoot durable delivery retry: lane=%s attempt=%s type=%s",
                             key.rsplit(":", 1)[-1], failures, type(error).__name__)
                await asyncio.sleep(min(30, 2 ** min(failures, 5)))

    async def healthy(self) -> bool:
        if not self.tasks or any(task.done() for task in self.tasks) or self.last_errors:
            return False
        try:
            return bool(await self.redis.ping())
        except Exception:  # noqa: BLE001 - health endpoint must not disclose connection details
            return False

    async def close(self) -> None:
        # Interrupted work is intentionally not acknowledged; next start resumes it.
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

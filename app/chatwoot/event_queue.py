"""Redis Streams inbox: acknowledge webhooks only after durable enqueueing.

One deployed agent-bot process, eight ordered lanes. A conversation always uses
the same lane; failed work stays pending and is retried, including after restart.
Redis already belongs to the Chatwoot contour and has AOF persistence enabled.

The optional snapshot is a separate Stream field, NEVER part of event payload.
Old readers ignore it and use their established GET path; new readers accept old
entries without it. Rollout/rollback needs no stream migration, drain or deletion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, replace

from redis.exceptions import ResponseError

from app.chatwoot.contracts import (
    BUTTON_SNAPSHOT_MAX_BYTES,
    ConversationChanged,
    IncomingChatwootMessage,
    MessageDeliveryChanged,
    StaffMessage,
    validated_button_snapshot,
)
from app.chatwoot.telemetry import event_timing

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
        if isinstance(event, IncomingChatwootMessage):
            # Preserve EXACT legacy event schema, even when a hint is present.
            payload = asdict(replace(event, button_snapshot=None))
            payload.pop("button_snapshot")
        else:
            payload = asdict(event)
            if isinstance(event, StaffMessage):
                # Keep ordinary/legacy staff events readable across rollback.
                # Only the new, explicitly provisioned commands need new fields.
                for field in ("consultation_aid_id", "occurred_at"):
                    if payload[field] is None:
                        payload.pop(field)
        fields = {"type": kind, "payload": json.dumps(payload, ensure_ascii=False)}
        if isinstance(event, IncomingChatwootMessage):
            snapshot = validated_button_snapshot(event.button_snapshot, event)
            if snapshot is not None:
                fields["button_snapshot"] = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        await self.redis.xadd(key, fields)

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
            batches = await self.redis.xreadgroup(
                self.group, self.consumer, {key: ">"}, count=1, block=block_ms
            )
        for _, messages in batches:
            for entry_id, fields in messages:
                event = EVENTS[fields["type"]](**json.loads(fields["payload"]))
                if isinstance(event, IncomingChatwootMessage):
                    hint = fields.get("button_snapshot")
                    try:
                        snapshot = (json.loads(hint) if isinstance(hint, str)
                                    and len(hint.encode("utf-8")) <= BUTTON_SNAPSHOT_MAX_BYTES else None)
                    except (ValueError, UnicodeError, RecursionError):
                        snapshot = None  # Broken optimization must not poison a lane.
                    event = replace(event, button_snapshot=validated_button_snapshot(snapshot, event))
                queue_age_ms = max(0, time.time() * 1000 - int(entry_id.split("-", 1)[0]))
                with event_timing(event, queue_age_ms=round(queue_age_ms, 3)):
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

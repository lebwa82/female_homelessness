"""A connector owns its Redis, queue, retry loop and process lease."""

import asyncio
import hashlib
import json
import logging

from redis.exceptions import LockError, ResponseError

logger = logging.getLogger(__name__)


class ConnectorQueue:
    def __init__(self, redis, service, *, namespace, lanes=4):
        self.redis, self.service = redis, service
        self.keys = tuple(f"{namespace}:events:{i}" for i in range(lanes))
        self.group, self.consumer = "connector", "single-instance"
        self.lease = redis.lock(f"{namespace}:lease", timeout=45, blocking_timeout=0)
        self.tasks = []
        self.errors = set()
        self.lease_ok = False
        self.lease_lost = asyncio.Event()

    async def enqueue(self, event):
        if self.lease_lost.is_set():
            raise RuntimeError("connector lease lost")
        # Conversation pointers suffice for outgoing events: no copy of their PII.
        identity = str(event.get("peer") or event["conversation_id"])
        lane = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], "big")
        key = self.keys[lane % len(self.keys)]
        await self.redis.xadd(key, {"payload": json.dumps(event, ensure_ascii=False)})

    async def start(self):
        if not await self.lease.acquire(blocking=False):
            raise RuntimeError("connector already running")
        self.lease_ok = True
        try:
            for key in self.keys:
                try:
                    await self.redis.xgroup_create(key, self.group, id="0", mkstream=True)
                except ResponseError as error:
                    if not str(error).startswith("BUSYGROUP"):
                        raise
            self.tasks = [asyncio.create_task(self.run(key)) for key in self.keys]
            self.tasks.append(asyncio.create_task(self.renew()))
        except BaseException:
            await self.close()
            raise

    async def renew(self):
        try:
            while True:
                await asyncio.sleep(10)
                await self.lease.extend(45, replace_ttl=True)
        except Exception as error:  # noqa: BLE001 - no connection strings in logs
            self.lease_ok = False
            self.lease_lost.set()
            logger.error("connector lease lost: type=%s", type(error).__name__)
            for task in self.tasks:
                if task is not asyncio.current_task():
                    task.cancel()

    async def consume_one(self, key, *, block_ms=1000):
        pending = await self.redis.xreadgroup(self.group, self.consumer, {key: "0"}, count=1)
        if not any(messages for _, messages in pending):
            pending = await self.redis.xreadgroup(self.group, self.consumer, {key: ">"},
                                                 count=1, block=block_ms)
        for _, messages in pending:
            for identity, fields in messages:
                await self.service.process(json.loads(fields["payload"]))
                async with self.redis.pipeline(transaction=True) as transaction:
                    transaction.xack(key, self.group, identity)
                    transaction.xdel(key, identity)
                    await transaction.execute()
                return True
        return False

    async def run(self, key):
        attempts = 0
        while self.lease_ok:
            try:
                await self.consume_one(key)
                self.errors.discard(key)
                attempts = 0
            except Exception as error:  # noqa: BLE001 - retain pending work, never log payload
                self.errors.add(key)
                attempts += 1
                logger.warning("connector retry: lane=%s type=%s", key.rsplit(":", 1)[-1],
                               type(error).__name__)
                await asyncio.sleep(min(30, 2 ** min(attempts, 5)))

    async def healthy(self):
        return (self.lease_ok and bool(self.tasks) and not self.errors
                and not any(task.done() for task in self.tasks) and bool(await self.redis.ping()))

    async def close(self):
        self.lease_ok = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        try:
            await self.lease.release()
        except LockError:
            pass

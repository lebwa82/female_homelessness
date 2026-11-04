"""Synthetic Lua integration in a disposable, network-isolated Redis container.

Run beside bot_monitor_probe.py with --network container:nevidimy-monitor-redis-test.
The only endpoint is loopback DB15. Existing data aborts without any deletion.
"""

from __future__ import annotations

import asyncio
import json

import bot_monitor_probe as probe
from redis.asyncio import Redis

URL = "redis://127.0.0.1:6379/15"
NOW = 1700001000
STREAMS = tuple(f"women-help:chatwoot-events:1:{lane}" for lane in range(8))
FIXTURE_KEYS = (*STREAMS, "queue:high", "queue:medium", "queue:low", "queues",
                "retry", "schedule")


async def reset(redis):
    await redis.delete(*FIXTURE_KEYS)
    for key in STREAMS:
        await redis.xgroup_create(key, "agent", "0", mkstream=True)


async def run() -> int:
    initialized = False
    passed = 0
    async with Redis.from_url(URL, decode_responses=True, socket_timeout=3,
                              socket_connect_timeout=3) as redis:
        try:
            if await redis.dbsize():
                raise ValueError("disposable_database_not_empty")
            initialized = True
            if int((await redis.info("server"))["redis_version"].split(".", 1)[0]) < 7:
                raise ValueError("redis_version_unsupported")

            async def observed():
                return await probe.check_queues(redis, {"account_id": "1"}, NOW)

            await reset(redis)
            assert await observed() == {"ok": True, "reason": "ok", "count": 0,
                                         "oldest_age_seconds": 0}
            passed += 1

            key = STREAMS[7]
            await redis.xadd(key, {"payload": "synthetic-pending"}, id="1700000800000-0")
            await redis.xreadgroup("agent", "synthetic", {key: ">"}, count=1)
            await redis.xadd(key, {"payload": "synthetic-ready"}, id="1700000900000-0")
            assert await observed() == {"ok": False, "reason": "queue_stalled", "count": 2,
                                         "oldest_age_seconds": 200}
            passed += 1

            for timestamp in (1700000800, 1700000800000):
                await reset(redis)
                await redis.lpush("queue:high", json.dumps({"enqueued_at": timestamp,
                                                           "args": ["synthetic-job"]}))
                assert await observed() == {"ok": False, "reason": "queue_stalled", "count": 1,
                                             "oldest_age_seconds": 200}
                passed += 1

            await reset(redis)
            await redis.zadd("schedule", {"synthetic-future-job": NOW + 1000})
            assert (await observed())["ok"] is True
            assert (await observed())["count"] == 0
            passed += 1

            await redis.zadd("retry", {"synthetic-due-job": NOW - 200})
            assert await observed() == {"ok": False, "reason": "queue_stalled", "count": 1,
                                         "oldest_age_seconds": 200}
            passed += 1

            for timestamp in (-1, NOW + 1):
                await reset(redis)
                await redis.lpush("queue:low", json.dumps({"enqueued_at": timestamp}))
                result = await observed()
                assert result == {"ok": False, "reason": "queue_metadata_invalid"}
                passed += 1

            await reset(redis)
            await redis.xgroup_destroy(STREAMS[0], "agent")
            assert await observed() == {"ok": False, "reason": "queue_group_missing"}
            passed += 1
            return passed
        finally:
            if initialized:
                await redis.delete(*FIXTURE_KEYS)


def main() -> int:
    try:
        passed = asyncio.run(asyncio.wait_for(run(), 35))
    except Exception as error:  # noqa: BLE001 - never output payloads or connection details
        print(json.dumps({"error": type(error).__name__}))
        return 1
    print(json.dumps({"passed": passed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

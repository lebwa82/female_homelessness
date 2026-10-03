"""Real isolated Redis; no production credentials, conversations, or network ports."""

import asyncio
import json
import shutil
import tempfile
import uuid

import pytest
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from test_chatwoot_app import FakeRequest, RecordingService, _payload

from app.chatwoot.app import AgentBotWebhook
from app.chatwoot.contracts import ConversationChanged, IncomingChatwootMessage
from app.chatwoot.event_queue import DurableEventQueue


@pytest.fixture
async def redis(tmp_path):
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server required for real queue integration test")
    # macOS AF_UNIX paths are limited to 104 bytes; pytest node names are longer.
    socket_directory = tempfile.TemporaryDirectory(prefix="fh-redis-", dir="/tmp")
    socket = socket_directory.name + "/redis.sock"
    process = await asyncio.create_subprocess_exec(
        executable, "--port", "0", "--unixsocket", socket, "--unixsocketperm", "700",
        "--dir", str(tmp_path), "--appendonly", "yes", "--appendfsync", "always",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    client = Redis(unix_socket_path=socket, decode_responses=True,
                   socket_connect_timeout=.2, socket_timeout=2, retry=Retry(NoBackoff(), 0))
    try:
        for _ in range(100):
            try:
                if await client.ping():
                    break
            except (OSError, ConnectionError):
                pass
            except Exception as error:
                if type(error).__name__ != "ConnectionError":
                    raise
            await asyncio.sleep(.02)
        else:
            pytest.fail("isolated Redis failed to start")
        yield client
    finally:
        await client.aclose()
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        socket_directory.cleanup()


async def prepared(redis, service, *, namespace=None):
    queue = DurableEventQueue(redis, service, namespace=namespace or uuid.uuid4().hex, lanes=2)
    # Initialize stream/group without launching workers; tests control each step.
    for key in queue.keys:
        if not await redis.exists(key):
            await redis.xgroup_create(key, queue.group, id="0", mkstream=True)
    return queue


@pytest.mark.asyncio
async def test_queued_events_survive_agent_restart_and_preserve_order(redis):
    service = RecordingService()
    first = await prepared(redis, service)
    events = [IncomingChatwootMessage(i, 2, 5, 1, "Synthetic input") for i in (1, 2)]
    for event in events:
        await first.enqueue(event)
    # Emulate process death after the stream delivered but before handler/ACK.
    await redis.xreadgroup(first.group, first.consumer, {first.keys[0]: ">"}, count=1)
    second = DurableEventQueue(redis, service, namespace=first.keys[0].rsplit(":", 1)[0], lanes=2)
    assert await second.consume_one(second.keys[0])
    assert await second.consume_one(second.keys[0])
    assert service.events == events
    assert await redis.xlen(second.keys[0]) == 0
    assert (await redis.xpending(second.keys[0], second.group))["pending"] == 0


@pytest.mark.asyncio
async def test_error_does_not_ack_and_another_lane_progresses(redis):
    class FailingService(RecordingService):
        fail = True

        async def process(self, event):
            if self.fail and event.conversation_id == 2:
                raise RuntimeError("synthetic_network_failure")
            return await super().process(event)

    service = FailingService()
    queue = await prepared(redis, service)
    await queue.enqueue(ConversationChanged(2))
    await queue.enqueue(ConversationChanged(3))
    with pytest.raises(RuntimeError, match="synthetic_network_failure"):
        await queue.consume_one(queue.keys[0])
    assert await redis.xlen(queue.keys[0]) == 1
    assert await queue.consume_one(queue.keys[1])
    service.fail = False
    assert await queue.consume_one(queue.keys[0])
    assert service.events == [ConversationChanged(3), ConversationChanged(2)]


@pytest.mark.asyncio
async def test_cancelled_worker_leaves_event_pending(redis):
    started = asyncio.Event()

    class WaitingService:
        async def process(self, event):
            started.set()
            await asyncio.Event().wait()

    queue = DurableEventQueue(redis, WaitingService(), namespace=uuid.uuid4().hex)
    await queue.start()
    try:
        assert await queue.healthy()
        await queue.enqueue(ConversationChanged(2))
        await asyncio.wait_for(started.wait(), 3)
    finally:
        await queue.close()
    assert await redis.xlen(queue.keys[2]) == 1
    service = RecordingService()
    resumed = DurableEventQueue(redis, service, namespace=queue.keys[0].rsplit(":", 1)[0])
    assert await resumed.consume_one(resumed.keys[2])
    assert service.events == [ConversationChanged(2)]
    assert not await queue.healthy()


@pytest.mark.asyncio
async def test_webhook_does_not_acknowledge_when_durable_enqueue_fails():
    class BrokenQueue:
        async def enqueue(self, event):
            raise ConnectionError("synthetic")

    service = RecordingService()
    webhook = AgentBotWebhook(service, route_secret="test", event_queue=BrokenQueue())
    reply = await webhook.handle(FakeRequest(json.dumps(_payload()).encode(), {}))
    assert reply.status == 503
    assert not service.events


@pytest.mark.asyncio
async def test_webhook_only_acknowledges_after_real_enqueue(redis):
    service = RecordingService()
    queue = await prepared(redis, service)
    webhook = AgentBotWebhook(service, route_secret="test", event_queue=queue)
    reply = await webhook.handle(FakeRequest(json.dumps(_payload()).encode(), {}))
    assert reply.status == 204 and not service.events
    assert await redis.xlen(queue.keys[1]) == 1
    await queue.consume_one(queue.keys[1])
    assert len(service.events) == 1


@pytest.mark.asyncio
async def test_running_worker_retries_and_recovers_health(redis):
    done = asyncio.Event()

    class RetryService:
        attempts = 0

        async def process(self, event):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synthetic_failure")
            done.set()

    service = RetryService()
    queue = await prepared(redis, service)
    await queue.start()  # Existing groups are intentionally reused.
    try:
        with pytest.raises(ValueError, match="unsupported_event"):
            await queue.enqueue(object())
        await queue.enqueue(ConversationChanged(2))
        for _ in range(100):
            if queue.last_errors:
                break
            await asyncio.sleep(.01)
        assert queue.last_errors and not await queue.healthy()
        await asyncio.wait_for(done.wait(), 4)
        for _ in range(100):
            if not queue.last_errors:
                break
            await asyncio.sleep(.01)
        assert await queue.healthy()
        assert service.attempts == 2
        assert await redis.xlen(queue.keys[0]) == 0
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_health_fails_when_redis_ping_fails():
    class OfflineRedis:
        async def ping(self):
            raise ConnectionError("synthetic")

    queue = DurableEventQueue(OfflineRedis(), RecordingService(), namespace="test")
    queue.tasks = [asyncio.create_task(asyncio.Event().wait())]
    try:
        assert not await queue.healthy()
    finally:
        await queue.close()

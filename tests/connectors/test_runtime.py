import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from redis.asyncio import Redis

from app.connectors.app import create_application
from app.connectors.http import ProviderError
from app.connectors.queue import ConnectorQueue
from app.connectors.service import ConnectorService
from app.connectors.vk import VK


async def test_outgoing_resumes_after_chatwoot_receipt_write_failure(settings_factory, redis):
    provider = SimpleNamespace(parts=lambda _: [{"text": "Тест"}, {"text": "Ещё"}],
                               send=AsyncMock(side_effect=["external-1", "external-2"]))
    cw = SimpleNamespace(outgoing=AsyncMock(return_value=("42", {})),
                         receipt=AsyncMock(side_effect=[ConnectionError("synthetic"), None]))
    service = ConnectorService(settings_factory(), redis, cw, provider)
    event = {"kind": "outgoing", "conversation_id": 7, "message_id": 11}
    with pytest.raises(ConnectionError):
        await service.process(event)
    await service.process(event)
    await service.process(event)
    assert provider.send.await_count == 2
    assert cw.receipt.await_count == 2
    assert cw.receipt.call_args.args == (7, 11, ["external-1", "external-2"])


async def test_outgoing_retries_only_unaccepted_part(settings_factory, redis):
    provider = SimpleNamespace(parts=lambda _: [{"text": "a"}, {"text": "b"}],
                               send=AsyncMock(side_effect=["first", ProviderError(429), "second"]))
    cw = SimpleNamespace(outgoing=AsyncMock(return_value=("42", {})), receipt=AsyncMock())
    service = ConnectorService(settings_factory(), redis, cw, provider)
    event = {"kind": "outgoing", "conversation_id": 7, "message_id": 11}
    with pytest.raises(ProviderError):
        await service.process(event)
    await service.process(event)
    assert provider.send.await_count == 3
    assert provider.send.call_args_list[1] == provider.send.call_args_list[2]
    cw.receipt.assert_awaited_once()


async def test_permanent_rejection_fails_message_and_does_not_block_queue(settings_factory, redis):
    provider = SimpleNamespace(parts=lambda _: [{"text": "a"}],
                               send=AsyncMock(side_effect=ProviderError(901, retryable=False)))
    cw = SimpleNamespace(outgoing=AsyncMock(return_value=("42", {})), receipt=AsyncMock(),
                         failed=AsyncMock())
    service = ConnectorService(settings_factory(), redis, cw, provider)
    event = {"kind": "outgoing", "conversation_id": 7, "message_id": 11}
    await service.process(event)
    await service.process(event)
    cw.failed.assert_awaited_once_with(7, 11, 901)
    cw.receipt.assert_not_awaited()
    assert provider.send.await_count == 1


async def test_repeated_incoming_is_not_sent_to_chatwoot_twice(settings_factory, redis):
    cw = SimpleNamespace(conversation=AsyncMock(return_value=7), incoming=AsyncMock())
    service = ConnectorService(settings_factory(), redis, cw, SimpleNamespace())
    event = {"kind": "incoming", "id": "11", "peer": "42", "text": "Тест"}
    await service.process(event)
    await service.process(event)
    cw.incoming.assert_awaited_once()
    # Message identity is scoped to the peer as well as the community.
    await service.process(event | {"peer": "43"})
    assert cw.incoming.await_count == 2


async def test_callback_filters_other_inboxes_private_and_incoming(
        settings_factory, http, client_factory):
    settings = settings_factory()
    queue = SimpleNamespace(enqueue=AsyncMock(), healthy=AsyncMock(return_value=True))
    client = await client_factory(create_application(settings, VK(settings, http), queue))
    path = "/webhooks/chatwoot/" + settings.callback_secret.get_secret_value()
    event = {"event": "message_created", "id": 11, "private": False, "message_type": "outgoing",
             "account": {"id": 1}, "inbox": {"id": 2}, "conversation": {"id": 7},
             "content": "canonical content must be re-read"}
    for update in ({"private": True}, {"message_type": "incoming"}, {"inbox": {"id": 3}},
                   {"account": {"id": 9}}, {"event": "message_updated"}):
        assert (await client.post(path, json=event | update)).status == 204
    queue.enqueue.assert_not_awaited()
    assert (await client.post(path + "bad", json=event)).status == 404
    assert (await client.post(path, json=event)).status == 204
    assert queue.enqueue.call_args.args[0] == {"kind": "outgoing", "message_id": 11,
                                              "conversation_id": 7}
    queue.enqueue.side_effect = ConnectionError()
    assert (await client.post(path, json=event)).status == 503
    assert (await client.get("/healthz")).status == 200
    queue.healthy.return_value = False
    assert (await client.get("/healthz")).status == 503


def test_runtime_imports_cannot_load_telegram_or_product_runtime():
    code = """
import importlib.abc
import sys
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith(('app.chatwoot', 'app.bot', 'app.config', 'app.service',
                                'app.agents', 'app.db', 'aiogram', 'openai', 'sqlalchemy')):
            raise RuntimeError('forbidden runtime import: ' + fullname)
sys.meta_path.insert(0, Boundary())
import app.connectors.__main__
from types import SimpleNamespace
from app.connectors.__main__ import load_provider
load_provider(SimpleNamespace(provider='vk'), None, None)
assert 'app.connectors.whatsapp' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


async def test_lease_loss_stops_acknowledgements_until_process_restart(monkeypatch):
    queue = ConnectorQueue(SimpleNamespace(lock=lambda *args, **kwargs: None),
                           SimpleNamespace(), namespace="synthetic")
    queue.lease = SimpleNamespace(extend=AsyncMock(side_effect=ConnectionError("synthetic")))
    queue.lease_ok = True
    monkeypatch.setattr("app.connectors.queue.asyncio.sleep", AsyncMock())
    await queue.renew()
    assert queue.lease_lost.is_set() and not queue.lease_ok
    with pytest.raises(RuntimeError, match="lease lost"):
        await queue.enqueue({"kind": "outgoing", "conversation_id": 7})


def test_compose_projects_do_not_reference_shared_runtime():
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "deploy/connectors/compose.yml").read_text())
    assert set(compose["services"]) == {"connector", "redis"}
    assert set(compose["volumes"]) == {"transport_data"}
    assert compose["services"]["redis"]["networks"] == ["transport"]
    assert compose["networks"]["transport"]["internal"] is True
    assert "external" not in json.dumps(compose)
    for service in compose["services"].values():
        assert service["mem_limit"] and service["cpus"]
    image = (root / "deploy/connectors/Containerfile").read_text()
    assert "COPY app/connectors" in image
    assert "COPY app ./app" not in image


@pytest.fixture
async def isolated_redis():
    url = os.environ.get("CONNECTOR_TEST_REDIS_URL")
    if not url:
        pytest.skip("set CONNECTOR_TEST_REDIS_URL to a disposable local Redis")
    client = Redis.from_url(url, decode_responses=True)
    await client.ping()
    yield client
    await client.aclose()


async def test_real_redis_pending_work_survives_worker_restart(isolated_redis):
    service = SimpleNamespace(process=AsyncMock(side_effect=[ConnectionError("synthetic"), None]))
    namespace = "test:" + uuid.uuid4().hex
    queue = ConnectorQueue(isolated_redis, service, namespace=namespace, lanes=1)
    key = queue.keys[0]
    await isolated_redis.xgroup_create(key, queue.group, id="0", mkstream=True)
    event = {"kind": "incoming", "peer": "42", "text": "Тест", "id": "11"}
    await queue.enqueue(event)
    try:
        with pytest.raises(ConnectionError):
            await queue.consume_one(key)
        assert (await isolated_redis.xpending(key, queue.group))["pending"] == 1
        resumed = ConnectorQueue(isolated_redis, service, namespace=namespace, lanes=1)
        assert await resumed.consume_one(key)
        assert await isolated_redis.xlen(key) == 0
        assert (await isolated_redis.xpending(key, queue.group))["pending"] == 0
        assert service.process.call_args.args == (event,)
    finally:
        await isolated_redis.delete(key)


async def test_real_redis_second_connector_cannot_steal_active_lease(isolated_redis):
    namespace = "test:" + uuid.uuid4().hex
    service = SimpleNamespace(process=AsyncMock())
    first = ConnectorQueue(isolated_redis, service, namespace=namespace, lanes=1)
    second = ConnectorQueue(isolated_redis, service, namespace=namespace, lanes=1)
    try:
        await first.start()
        assert await first.healthy()
        with pytest.raises(RuntimeError, match="already running"):
            await second.start()
        await first.enqueue({"kind": "outgoing", "conversation_id": 7, "message_id": 11})
        for _ in range(100):
            if service.process.await_count:
                break
            await asyncio.sleep(.01)
        assert service.process.await_count == 1
    finally:
        await first.close()
        await isolated_redis.delete(*first.keys)


async def test_real_whatsapp_statuses_wait_for_all_parts_and_never_regress(
        isolated_redis, settings_factory):
    settings = settings_factory("whatsapp", whatsapp_phone_id=str(uuid.uuid4().int))
    cw = SimpleNamespace(outgoing=AsyncMock(return_value=("79990000001", {})),
                         receipt=AsyncMock(), failed=AsyncMock(), delivery_status=AsyncMock())
    provider = SimpleNamespace(parts=lambda _: [{"text": "a"}, {"text": "b"}],
                               send=AsyncMock(side_effect=["wamid.1", "wamid.2"]))
    service = ConnectorService(settings, isolated_redis, cw, provider)
    event = {"kind": "outgoing", "conversation_id": 7, "message_id": 11}
    try:
        # A provider status can arrive before the send HTTP response is stored.
        await service.status({"id": "wamid.1", "status": "delivered"})
        await service.process(event)
        cw.delivery_status.assert_not_awaited()
        await service.status({"id": "wamid.2", "status": "delivered"})
        cw.delivery_status.assert_awaited_once_with(7, 11, "delivered")
        await service.status({"id": "wamid.1", "status": "read"})
        await service.status({"id": "wamid.2", "status": "read"})
        await service.status({"id": "wamid.1", "status": "sent"})
        assert cw.delivery_status.call_args.args == (7, 11, "read")
        await service.status({"id": "wamid.2", "status": "failed"})
        cw.failed.assert_awaited_once_with(7, 11, "async")
    finally:
        keys = [key async for key in isolated_redis.scan_iter(match=settings.scope + ":*")]
        if keys:
            await isolated_redis.delete(*keys)

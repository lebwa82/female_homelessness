"""Synthetic monitoring fixtures only: no real tokens or network access."""

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.monitoring.state import REASONS
from scripts import bot_monitor_probe as probe


def config(**changes):
    return probe.validate_config({
        "redis_url": "redis://:synthetic-password@redis:6379/0",
        "account_id": 1,
        "telegram_bot_token": "12345678:synthetic_bot_token",
        "proxy_url": probe.PROXY_URL,
        "agent_health_url": probe.AGENT_URL,
        "ca_file": "/synthetic/ca.crt",
        **changes,
    })


def test_probe_reason_vocabulary_matches_state_contract():
    tree = ast.parse(Path(probe.__file__).read_text())
    def literals(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return literals(node.body) | literals(node.orelse)
        return set()
    reasons = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "observation" and len(node.args) > 1):
            reasons.update(literals(node.args[1]))
    reasons.update(re.findall(r"fail\('([^']+)'\)", probe.QUEUE_PROJECTION))
    assert reasons <= REASONS


@pytest.mark.parametrize(("stamp", "now", "ok", "reason"), [
    ("1000", 1000, True, "ok"),
    (b"1000", 1119, True, "ok"),
    ("1000", 1120, False, "poll_stale"),
    ("1001", 1000, False, "poll_invalid"),
    ("-1", 1000, False, "poll_invalid"),
    ("1.5", 1000, False, "poll_invalid"),
    (None, 1000, False, "poll_invalid"),
    ("credential-marker", 1000, False, "poll_invalid"),
])
def test_poll_age(stamp, now, ok, reason):
    result = probe.age_observation(stamp, now, max_age=120)
    assert result["ok"] is ok
    assert result["reason"] == reason
    assert "credential-marker" not in json.dumps(result)


async def test_poll_reads_only_numeric_bot_identity_key():
    redis = AsyncMock()
    redis.get.return_value = "1000"
    assert (await probe.check_poll(redis, config(), 1000))["ok"]
    redis.get.assert_awaited_once_with("women-help:telegram-ingress:12345678:last_poll")


@pytest.mark.parametrize("changes", [
    {"proxy_url": "http://other-proxy:3128"},
    {"proxy_url": ""},
    {"agent_health_url": "http://agent-bot:8080/healthz"},
    {"agent_health_url": "https://other-host/healthz"},
    {"telegram_bot_token": "1234:bad/token"},
    {"account_id": "1:other"},
    {"account_id": True},
    {"redis_url": "https://redis/"},
    {"ca_file": "relative.crt"},
    {"telegram_events": [{"event_id": "one", "text": "synthetic"}]},
    {"telegram_events": [{}] * 6},
    {"unexpected": "synthetic"},
])
def test_strict_config(changes):
    with pytest.raises(ValueError):
        config(**changes)


@pytest.mark.parametrize(("data", "ok", "reason"), [
    ({"count": 0, "oldest": 1000}, True, "ok"),
    ({"count": 1, "oldest": 820}, True, "ok"),
    ({"count": 2, "oldest": 819}, False, "queue_stalled"),
    ({"count": 1, "oldest": 1001}, False, "queue_metadata_invalid"),
    ({"count": 1, "oldest": -1}, False, "queue_metadata_invalid"),
    ({"count": 0, "oldest": float("nan")}, False, "queue_metadata_invalid"),
    ({"error": "queue_group_missing"}, False, "queue_group_missing"),
    ({"error": "secret-error-marker"}, False, "queue_metadata_invalid"),
    ({"count": 0, "oldest": 1000, "raw": "secret-marker"}, False,
     "queue_metadata_invalid"),
])
def test_queue_observation_never_converts_unknown_to_zero_success(data, ok, reason):
    result = probe.queue_observation(data, 1000)
    assert result["ok"] is ok
    assert result["reason"] == reason
    assert "secret" not in json.dumps(result)


async def test_queues_single_bounded_projection_all_eight_lanes():
    redis = AsyncMock()
    redis.eval.return_value = '{"count":3,"oldest":819}'
    result = await probe.check_queues(redis, config(), 1000)
    assert result == {"ok": False, "reason": "queue_stalled", "count": 3,
                      "oldest_age_seconds": 181}
    args = redis.eval.call_args.args
    assert args[0] == probe.QUEUE_PROJECTION
    assert args[1] == 8
    assert args[2:10] == tuple(f"women-help:chatwoot-events:1:{i}" for i in range(8))
    assert args[10] == 1000
    assert redis.eval.await_count == 1
    redis.get.assert_not_called()


class Response:
    def __init__(self, data=None, status=200):
        self.status = status
        self.content = AsyncMock()
        self.content.readexactly.side_effect = asyncio.IncompleteReadError(
            json.dumps(data).encode(), 16385)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return next(self.responses)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return next(self.responses)


@pytest.mark.parametrize(("data", "ok"), [
    ({"ok": True, "result": {"id": 12345678, "is_bot": True}}, True),
    ({"ok": True, "result": {"id": 12345679, "is_bot": True}}, False),
    ({"ok": True, "result": {"id": "12345678", "is_bot": True}}, False),
    ({"ok": True, "result": {"id": 12345678, "is_bot": False}}, False),
    ({"ok": False, "description": "secret-marker"}, False),
])
async def test_getme_identity_and_forced_proxy(data, ok):
    session = Session([Response(data)])
    result = await probe.check_proxy(session, config())
    assert result["ok"] is ok
    assert "secret-marker" not in json.dumps(result)
    [(method, url, kwargs)] = session.calls
    assert method == "post"
    assert url == "https://api.telegram.org/bot12345678:synthetic_bot_token/getMe"
    assert kwargs == {"proxy": probe.PROXY_URL, "json": None, "allow_redirects": False}


async def test_redirect_never_followed_or_retried_without_proxy():
    session = Session([Response(status=302), Response(status=302)])
    assert not (await probe.check_proxy(session, config()))["ok"]
    assert not (await probe.check_agent(session, config()))["ok"]
    assert len(session.calls) == 2
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert session.calls[0][2]["proxy"] == probe.PROXY_URL
    assert session.calls[1][1] == probe.AGENT_URL


async def test_delivery_uses_separate_token_same_proxy_and_returns_only_event_id():
    settings = config(telegram_alert_token="98765432:synthetic_admin_token",
                      telegram_alert_chat_id=-123,
                      telegram_events=[{"event_id": "event-1", "text": "synthetic event"}])
    session = Session([Response({"ok": True, "result": {"message_id": 1}})])
    assert await probe.deliver_event(session, settings, settings["telegram_events"][0]) == "event-1"
    assert session.calls[0][1].endswith("98765432:synthetic_admin_token/sendMessage")
    assert session.calls[0][2]["proxy"] == probe.PROXY_URL


async def test_errors_are_bounded_and_do_not_echo_credentials():
    async def failed():
        raise RuntimeError("redis://:synthetic-password@redis; secret-token")
    assert await probe.bounded_check(failed(), "redis_unavailable") == {
        "ok": False, "reason": "redis_unavailable"}


async def test_timeout_is_failure(monkeypatch):
    monkeypatch.setattr(probe, "DEADLINE", 0.01)
    assert await probe.bounded_check(asyncio.sleep(1), "unavailable") == {
        "ok": False, "reason": "probe_timeout"}


async def test_tls_failure_still_checks_redis_and_cannot_deliver(monkeypatch):
    redis = AsyncMock()
    redis.__aenter__.return_value = redis
    redis.get.return_value = "1000"
    redis.eval.return_value = '{"count":0,"oldest":1000}'
    monkeypatch.setattr(probe.Redis, "from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr(probe.time, "time", lambda: 1000)
    def failed_tls(**kwargs):
        raise OSError("sensitive-path-marker")
    monkeypatch.setattr(probe.ssl, "create_default_context", failed_tls)
    result = await probe.probe(config(
        telegram_alert_token="98765432:synthetic_admin_token", telegram_alert_chat_id=10,
        telegram_events=[{"event_id": "event-1", "text": "synthetic event"}]))
    assert result["observations"]["telegram_poll"]["ok"]
    assert result["observations"]["queues"]["ok"]
    assert not result["observations"]["agent"]["ok"]
    assert result["telegram_failed"] is True
    assert "sensitive" not in json.dumps(result)


async def test_independent_observations_and_alert_failure(monkeypatch):
    redis = AsyncMock()
    redis.__aenter__.return_value = redis
    redis.get.side_effect = RuntimeError("synthetic-password")
    redis.eval.return_value = '{"count":0,"oldest":1000}'
    monkeypatch.setattr(probe.Redis, "from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr(probe.time, "time", lambda: 1000)
    context = Mock()
    connectors = []
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: context)
    monkeypatch.setattr(probe.aiohttp, "TCPConnector", lambda **kwargs: (connectors.append(kwargs),
                                                                      object())[1])
    session = Session([Response({"ok": True, "result": {"id": 12345678, "is_bot": True}}),
                       Response(), Response({"ok": False, "description": "secret-marker"})])
    @asynccontextmanager
    async def client_session(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"].total == 8
        yield session
    monkeypatch.setattr(probe.aiohttp, "ClientSession", client_session)
    result = await probe.probe(config(
        telegram_alert_token="98765432:synthetic_admin_token", telegram_alert_chat_id=10,
        telegram_events=[{"event_id": "event-1", "text": "synthetic event"}]))
    assert connectors == [{"ssl": context}]
    context.load_verify_locations.assert_called_once_with(cafile="/synthetic/ca.crt")
    assert not result["observations"]["telegram_poll"]["ok"]
    assert all(result["observations"][name]["ok"] for name in ("agent", "queues", "telegram_proxy"))
    assert result["telegram_delivered"] == []
    assert result["telegram_failed"] is True
    assert "secret" not in json.dumps(result)


def test_cli_invalid_input_never_prints_supplied_secret():
    child = subprocess.run([sys.executable, "-m", "scripts.bot_monitor_probe"],
                           input='{"secret":"synthetic-password"}', text=True,
                           capture_output=True, check=True)
    result = json.loads(child.stdout)
    assert set(result["observations"]) == set(probe.CHECKS)
    assert all(not value["ok"] for value in result["observations"].values())
    assert len(child.stdout) < 1024
    assert "synthetic-password" not in child.stdout
    assert child.stderr == ""


@pytest.fixture
async def real_redis():
    """Redis 7, local executable or explicitly selected disposable loopback DB15."""
    url = os.environ.get("TEST_MONITOR_REDIS_URL")
    process = None
    directory = None
    initialized = False
    if url:
        if not re.fullmatch(r"redis://127\.0\.0\.1:[0-9]{1,5}/15", url):
            pytest.fail("TEST_MONITOR_REDIS_URL must select local disposable Redis DB15")
        redis = Redis.from_url(url, decode_responses=True,
                               socket_connect_timeout=0.2, socket_timeout=2)
    else:
        executable = shutil.which("redis-server")
        if executable is None:
            pytest.skip("redis-server or local TEST_MONITOR_REDIS_URL required for Lua integration")
        # macOS AF_UNIX sockets have a short path limit; pytest paths exceed it.
        directory = tempfile.TemporaryDirectory(prefix="monitor-redis-", dir="/tmp")
        socket = directory.name + "/redis.sock"
        process = await asyncio.create_subprocess_exec(
            executable, "--port", "0", "--unixsocket", socket,
            "--save", "", "--appendonly", "no",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        redis = Redis(unix_socket_path=socket, decode_responses=True,
                      socket_connect_timeout=0.2, socket_timeout=2)
    try:
        for _ in range(100):
            try:
                if await redis.ping():
                    break
            except RedisConnectionError:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("synthetic Redis did not start")
        version = (await redis.info("server"))["redis_version"]
        if int(version.split(".", 1)[0]) < 7:
            pytest.fail("Redis >=7 required for consumer-group lag projection")
        if await redis.dbsize():
            pytest.fail("Lua integration requires an empty disposable Redis database")
        initialized = True
        for lane in range(8):
            await redis.xgroup_create(f"women-help:chatwoot-events:1:{lane}", "agent", "0",
                                      mkstream=True)
        yield redis
    finally:
        if initialized:
            await redis.delete(*(f"women-help:chatwoot-events:1:{lane}" for lane in range(8)),
                               "queue:high", "queue:medium", "queue:low", "queues",
                               "retry", "schedule")
        await redis.aclose()
        if process is not None:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        if directory is not None:
            directory.cleanup()


async def test_lua_empty_streams_healthy(real_redis):
    result = await probe.check_queues(real_redis, config(), 1700001000)
    assert result == {"ok": True, "reason": "ok", "count": 0, "oldest_age_seconds": 0}


@pytest.mark.parametrize("timestamp", [1700000800, 1700000800000])
async def test_lua_sidekiq_seconds_and_milliseconds(real_redis, timestamp):
    await real_redis.lpush("queue:high", json.dumps({"enqueued_at": timestamp,
                                                   "args": ["secret-payload-marker"]}))
    result = await probe.check_queues(real_redis, config(), 1700001000)
    assert result == {"ok": False, "reason": "queue_stalled", "count": 1,
                      "oldest_age_seconds": 200}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("key", ["retry", "schedule"])
async def test_lua_due_jobs_only(real_redis, key):
    await real_redis.zadd(key, {"secret-due-job": 1700000800,
                                "secret-future-job": 1700002000})
    result = await probe.check_queues(real_redis, config(), 1700001000)
    assert result["count"] == 1
    assert result["oldest_age_seconds"] == 200
    assert not result["ok"]


async def test_lua_pending_and_unread_all_lanes(real_redis):
    key = "women-help:chatwoot-events:1:7"
    await real_redis.xadd(key, {"payload": "secret-pending"}, id="1700000800000-0")
    await real_redis.xreadgroup("agent", "synthetic", {key: ">"}, count=1)
    await real_redis.xadd(key, {"payload": "secret-ready"}, id="1700000900000-0")
    result = await probe.check_queues(real_redis, config(), 1700001000)
    assert result["count"] == 2
    assert result["oldest_age_seconds"] == 200
    assert not result["ok"]


async def test_lua_missing_group_and_invalid_timestamp_fail(real_redis):
    key = "women-help:chatwoot-events:1:0"
    await real_redis.xgroup_destroy(key, "agent")
    assert (await probe.check_queues(real_redis, config(), 1700001000))["reason"] == "queue_group_missing"
    await real_redis.xgroup_create(key, "agent", "0")
    await real_redis.lpush("queue:low", '{"enqueued_at":-1,"args":["secret-marker"]}')
    result = await probe.check_queues(real_redis, config(), 1700001000)
    assert not result["ok"]
    assert result["reason"] == "queue_metadata_invalid"


async def test_lua_queue_discovery_is_bounded(real_redis):
    await real_redis.sadd("queues", *(f"synthetic_{i}" for i in range(65)))
    result = await probe.check_queues(real_redis, config(), 1700001000)
    assert result == {"ok": False, "reason": "queue_metadata_invalid"}

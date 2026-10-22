import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("timeweb_proxy_latency_probe.py")
TOKEN = "8831692160:" + "synthetic_token_" * 3
PROXY = "http://telegram-proxy:3128"


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("proxy_latency_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSession:
    def __init__(self, *, trace_configs, **kwargs):
        self.trace = trace_configs[0]
        self.options = kwargs
        self.calls = []
        self.active = self.maximum_active = self.consumed = 0
        self.failure = None
        self.status = 200
        self.payload = {"ok": True, "result": {"id": 8831692160, "is_bot": True}}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        assert self.active == 0

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Request(self, kwargs["trace_request_ctx"], len(self.calls) > 1)


class Request:
    def __init__(self, session, attempt, reused):
        self.session, self.attempt, self.reused = session, attempt, reused

    async def __aenter__(self):
        session = self.session
        session.active += 1
        session.maximum_active = max(session.maximum_active, session.active)
        context = SimpleNamespace(trace_request_ctx=self.attempt)
        events = ([session.trace.on_connection_reuseconn] if self.reused else
                  [session.trace.on_connection_create_start, session.trace.on_connection_create_end])
        for event in events:
            for callback in event:
                await callback(session, context, None)
        if session.failure:
            session.active -= 1
            raise session.failure
        body = json.dumps(session.payload).encode()

        async def first(size):
            assert size == 1
            return body[:1]

        async def rest():
            session.consumed += 1
            return body[1:]

        return SimpleNamespace(status=session.status, content=SimpleNamespace(read=first), read=rest)

    async def __aexit__(self, *args):
        self.session.active -= 1


@pytest.mark.asyncio
async def test_ten_sequential_verified_proxy_requests_reuse_and_consume(probe, monkeypatch):
    sessions, connectors = [], []

    def connector(**kwargs):
        connectors.append(kwargs)
        return object()

    def session(**kwargs):
        result = FakeSession(**kwargs)
        sessions.append(result)
        return result

    monkeypatch.setattr(probe.aiohttp, "TCPConnector", connector)
    monkeypatch.setattr(probe.aiohttp, "ClientSession", session)
    report = await probe.measure(TOKEN, PROXY)
    assert report["ok"] and len(report["samples"]) == 10
    assert set(report) == {"ok", "read_only", "messages_sent", "polling_started", "samples",
                           "first_byte_definition", "expected_identity"}
    assert all(set(row) == {"ok", "connect_ms", "connection_reused", "first_byte_ms", "total_ms"}
               for row in report["samples"])
    client = sessions[0]
    assert len(sessions) == client.maximum_active == 1
    assert client.consumed == len(client.calls) == 10
    assert client.options["trust_env"] is False
    assert connectors[0]["limit"] == connectors[0]["limit_per_host"] == 1
    assert connectors[0]["ssl"].check_hostname
    assert connectors[0]["ssl"].verify_mode == probe.ssl.CERT_REQUIRED
    assert [row["connection_reused"] for row in report["samples"]] == [False] + [True] * 9
    for url, options in client.calls:
        assert url == f"https://api.telegram.org/bot{TOKEN}/getMe"
        assert options["proxy"] == PROXY and options["allow_redirects"] is False
    assert all(row["total_ms"] >= row["first_byte_ms"] >= 0 for row in report["samples"])
    assert TOKEN not in json.dumps(report) and PROXY not in json.dumps(report)


@pytest.mark.asyncio
async def test_request_failure_never_outputs_secret_exception_text(probe, monkeypatch):
    session = None

    def create(**kwargs):
        nonlocal session
        session = FakeSession(**kwargs)
        session.failure = probe.aiohttp.ClientError(TOKEN + PROXY)
        return session

    monkeypatch.setattr(probe.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(probe.aiohttp, "ClientSession", create)
    report = await probe.measure(TOKEN, PROXY)
    assert report["ok"] is False and len(session.calls) == 10
    assert all(row["error_type"] == "ClientError" for row in report["samples"])
    assert session.maximum_active == 1
    assert TOKEN not in json.dumps(report) and PROXY not in json.dumps(report)


@pytest.mark.parametrize("proxy", ["", "http://other:3128", "https://telegram-proxy:3128"])
def test_proxy_is_mandatory_exact_and_no_error_text_leaks(probe, monkeypatch, capsys, proxy):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_PROXY_URL", proxy)
    assert probe.main() == 1
    output = capsys.readouterr()
    assert not output.err and output.out.startswith("PROXY_PROBE=")
    report = json.loads(output.out.removeprefix("PROXY_PROBE="))
    assert report["error_type"] == "ConfigurationError" and report["samples"] == []
    assert TOKEN not in output.out and "other" not in output.out


def test_unknown_top_level_error_is_type_sanitized(probe, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_PROXY_URL", PROXY)

    async def fail(*args):
        raise RuntimeError(TOKEN + PROXY)

    monkeypatch.setattr(probe, "measure", fail)
    assert probe.main() == 1
    output = capsys.readouterr()
    report = json.loads(output.out.removeprefix("PROXY_PROBE="))
    assert report["error_type"] == "ProbeError"
    assert TOKEN not in output.out and PROXY not in output.out and not output.err


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,error", [
    (302, {"ok": True}, "ResponseError"),
    (200, [], "ResponseError"),
    (200, {"ok": True, "result": {"id": 1, "is_bot": True}}, "IdentityError"),
])
async def test_bad_response_is_consumed_and_sanitized(probe, monkeypatch, status, payload, error):
    session = None

    def create(**kwargs):
        nonlocal session
        session = FakeSession(**kwargs)
        session.status, session.payload = status, payload
        return session

    monkeypatch.setattr(probe.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(probe.aiohttp, "ClientSession", create)
    report = await probe.measure(TOKEN, PROXY)
    assert not report["ok"] and not report["expected_identity"]
    assert session.consumed == 10 and session.maximum_active == 1
    assert all(row["error_type"] == error for row in report["samples"])

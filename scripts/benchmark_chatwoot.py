"""Compare a Git baseline with the working tree on an isolated HTTPS API stub.

No production credentials, customer data, Telegram, or LLM calls. The real
scenario/service/client code runs against neutral paginated API data. Injected
per-request latency is a controlled experiment, not a production prediction.
Run: python -m scripts.benchmark_chatwoot --samples 10 --request-delay-ms 20
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import ssl
import subprocess
import sys
import tempfile
import time
import types
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from app.chatwoot import client, followups, service
from app.chatwoot.contracts import IncomingChatwootMessage
from app.transport_tls import server_context
from scripts.prepare_internal_tls import prepare

BASELINE = "b7a4c460e3e5301f8363c4cb50c2e36e8a4c0511"
NOW = datetime(2026, 10, 22, 9, tzinfo=UTC)


def baseline_module(ref, name):
    source = subprocess.check_output(["git", "show", f"{ref}:app/chatwoot/{name}.py"])
    module = types.ModuleType(f"_chatwoot_baseline_{name}")
    sys.modules[module.__name__] = module
    # Execute only the explicitly selected local Git revision, never API data.
    exec(compile(source, f"git:{ref}:{name}", "exec"), module.__dict__)  # noqa: S102
    return module


class NoModel:
    async def evaluate(self, _context):
        raise AssertionError("Menu benchmark must not call a model")


class ApiStub:
    def __init__(self, delay):
        self.delay = delay
        self.calls = Counter()
        self.connections = set()
        self.timer_mode = False
        self.reply_at = None
        self.reset(10)

    @staticmethod
    def conversation(cid):
        return {"id": cid, "status": "pending", "assignee_id": None,
                "custom_attributes": {"reply_owner": "bot", "ownership_version": 3,
                                      "workflow_state": "greeting",
                                      "scenario": {"screen": "s1", "variant": 0}}}

    def reset(self, size):
        self.record = self.conversation(23)
        self.messages = [{"id": n, "message_type": "incoming", "private": False,
                          "content": "Synthetic history", "content_attributes": {},
                          "created_at": NOW.isoformat()} for n in range(1, size + 1)]
        self.reply_at = None

    async def handle(self, request):
        self.connections.add(request.transport)
        await asyncio.sleep(self.delay)
        path, method = request.path, request.method
        if path.endswith("/conversations"):
            self.calls["list"] += 1
            page = int(request.query["page"])
            records = []
            for cid in range(1, 112):
                item = self.conversation(cid)
                item["custom_attributes"]["scenario_followups"] = {"job": {
                    "state": "pending", "due_at": (NOW + timedelta(days=1)).isoformat(),
                    "screen": "s5", "context": {},
                }}
                records.append(item)
            return web.json_response({"data": {"payload": records[(page - 1) * 20:page * 20]}})
        cid = int(path.split("/conversations/")[1].split("/")[0])
        if self.timer_mode:
            self.calls["conversation"] += 1
            item = self.conversation(cid)
            item["custom_attributes"]["scenario_followups"] = {"job": {
                "state": "pending", "due_at": (NOW + timedelta(days=1)).isoformat(),
                "screen": "s5", "context": {},
            }}
            return web.json_response(item)
        if path.endswith("/messages") and method == "GET":
            self.calls["history_pages"] += 1
            if "after" in request.query:
                after = int(request.query["after"])
                # Same filtering/order/limit as the installed MessageFinder.
                page = sorted((m for m in self.messages if m["id"] > after),
                              key=lambda m: m.get("created_at", NOW.isoformat()))[:100]
                return web.json_response({"payload": page})
            before = int(request.query.get("before", sys.maxsize))
            page = sorted((m for m in self.messages if m["id"] < before),
                          key=lambda m: m["id"], reverse=True)[:20]
            return web.json_response({"payload": list(reversed(page))})
        if path.endswith("/messages") and method == "POST":
            self.calls["reply"] += 1
            message = {**await request.json(), "id": max(m["id"] for m in self.messages) + 1}
            self.messages.append(message)
            self.reply_at = time.perf_counter()
            return web.json_response(message)
        if path.endswith("/custom_attributes"):
            self.calls["attributes"] += 1
            self.record["custom_attributes"].update((await request.json())["custom_attributes"])
        else:
            self.calls["conversation"] += 1
        return web.json_response(copy.deepcopy(self.record))


@contextmanager
def test_ca(directory):
    original = aiohttp.ClientSession
    context = ssl.create_default_context(cafile=str(directory / "ca.crt"))

    def session(**kwargs):
        return original(connector=aiohttp.TCPConnector(ssl=context), **kwargs)

    with patch.object(aiohttp, "ClientSession", session):
        yield


def distribution(values):
    values = sorted(values)
    return {"p50": round(values[len(values) // 2], 2),
            "p95": round(values[math.ceil(len(values) * .95) - 1], 2)}


async def measure(url, stub, modules, samples, *, pooled=False):
    client_module, service_module, followup_module = modules
    transport = client_module.AiohttpChatwootTransport(
        url, **({"reuse_connections": True} if pooled else {}),
    )
    api = client_module.ChatwootClient(base_url=url, account_id=1, read_token="synthetic-read",
                                      bot_token="synthetic-bot", transport=transport)
    results = {}
    try:
        for size in (10, 100):
            stub.calls.clear()
            stub.connections.clear()
            created, total = [], []
            for _ in range(samples):
                stub.reset(size)
                bot = service_module.ChatwootAgentService(api, gateway=NoModel())
                bot._scenario_effects.clock = lambda: NOW
                started = time.perf_counter()
                assert await bot.process(IncomingChatwootMessage(size, 23, 7, 5, "continue"))
                total.append((time.perf_counter() - started) * 1000)
                created.append((stub.reply_at - started) * 1000)
            results[str(size)] = {
                "reply_created_ms": distribution(created), "event_complete_ms": distribution(total),
                "requests_per_event": sum(stub.calls.values()) / samples,
                "history_pages_per_event": stub.calls["history_pages"] / samples,
                "connections_total": len(stub.connections),
            }
        stub.timer_mode = True
        stub.calls.clear()
        bot = service_module.ChatwootAgentService(api, gateway=NoModel())
        bot._scenario_effects.clock = lambda: NOW
        started = time.perf_counter()
        kwargs = {"now": NOW} if followup_module is followups else {}
        assert await followup_module.sweep(api, bot, **kwargs) == 0
        results["scheduler_111_future_jobs"] = {
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "requests": dict(stub.calls),
        }
        stub.timer_mode = False
    finally:
        if pooled:
            await transport.aclose()
    return results


async def benchmark(args):
    baseline = tuple(baseline_module(args.baseline_ref, name)
                     for name in ("client", "service", "followups"))
    stub = ApiStub(args.request_delay_ms / 1000)
    with tempfile.TemporaryDirectory(prefix="nevidimy-benchmark-") as temporary:
        directory = Path(temporary)
        prepare(directory)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", stub.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context(
            str(directory / "chatwoot.crt"), str(directory / "chatwoot.key"),
        ))
        try:
            await site.start()
            url = f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            with test_ca(directory):
                before = await measure(url, stub, baseline, args.samples)
                without_pool = await measure(url, stub, (client, service, followups), args.samples)
                after = await measure(url, stub, (client, service, followups), args.samples, pooled=True)
        finally:
            await runner.cleanup()
    print(json.dumps({"environment": "local_synthetic_verified_https", "baseline": args.baseline_ref,
                      "samples": args.samples, "injected_request_delay_ms": args.request_delay_ms,
                      "before": before, "without_pool": without_pool, "after": after}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--request-delay-ms", type=float, default=20)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not 0 <= args.request_delay_ms <= 100:
        parser.error("samples must be 1..100 and request delay 0..100 ms")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()

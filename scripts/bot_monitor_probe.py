"""Standalone, read-only health observations; stdin/stdout contain bounded JSON.

This module deliberately does not import application configuration or bot code.
Only optional, explicitly supplied administrator notifications mutate anything.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import ssl
import sys
import time
from urllib.parse import urlsplit

import aiohttp
from redis.asyncio import Redis

PROXY_URL = "http://telegram-proxy:3128"
AGENT_URL = "https://agent-bot:8080/healthz"
DEADLINE = 8
MAX_INPUT = 65536
TOKEN = re.compile(r"([1-9][0-9]{0,19}):[A-Za-z0-9_-]{10,200}\Z")
EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
CHECKS = ("telegram_poll", "telegram_proxy", "agent", "queues")

# Payloads never leave Redis: only numeric counts/timestamps and fixed errors do.
# Redis 7 XINFO lag distinguishes unread events from acknowledged retained entries.
QUEUE_PROJECTION = r"""
local now = tonumber(ARGV[1])
local function fail(reason) return cjson.encode({error=reason}) end
local function stamp(value, millis)
  local n = tonumber(value)
  if not n or n ~= n or n <= 0 or n == math.huge then return nil end
  if millis or n > 100000000000 then n = n / 1000 end
  if n > now then return nil end
  return n
end
local function idstamp(id)
  if type(id) ~= 'string' then return nil end
  local ms = string.match(id, '^(%d+)%-%d+$')
  return ms and stamp(ms, true) or nil
end
local count, oldest = 0, now
local function include(n, t)
  count = count + n
  if n > 0 and t < oldest then oldest = t end
end
for lane = 1, 8 do
  if redis.call('EXISTS', KEYS[lane]) == 0 then return fail('queue_group_missing') end
  local groups = redis.call('XINFO', 'GROUPS', KEYS[lane])
  local group = nil
  for _, fields in ipairs(groups) do
    local row = {}
    for i = 1, #fields, 2 do row[fields[i]] = fields[i+1] end
    if row.name == 'agent' then group = row end
  end
  if not group then return fail('queue_group_missing') end
  if type(group.lag) ~= 'number' or group.lag < 0 then
    return fail('queue_metadata_invalid')
  end
  local pending = redis.call('XPENDING', KEYS[lane], 'agent')
  if pending[1] > 0 then
    local t = idstamp(pending[2])
    if not t then return fail('queue_metadata_invalid') end
    include(pending[1], t)
  end
  if group.lag > 0 then
    local ready = redis.call('XRANGE', KEYS[lane], '(' .. group['last-delivered-id'],
                             '+', 'COUNT', 1)
    if #ready ~= 1 then return fail('queue_metadata_invalid') end
    local t = idstamp(ready[1][1])
    if not t then return fail('queue_metadata_invalid') end
    include(group.lag, t)
  end
end
if redis.call('SCARD', 'queues') > 64 then return fail('queue_metadata_invalid') end
local names, seen = {'high', 'medium', 'low'}, {high=true, medium=true, low=true}
for _, name in ipairs(redis.call('SMEMBERS', 'queues')) do
  if #name > 64 or not string.match(name, '^[%w_:%-]+$') then
    return fail('queue_metadata_invalid')
  end
  if not seen[name] then names[#names+1] = name; seen[name] = true end
end
if #names > 64 then return fail('queue_metadata_invalid') end
for _, name in ipairs(names) do
  local key = 'queue:' .. name
  local n = redis.call('LLEN', key)
  if n > 0 then
    local raw = redis.call('LINDEX', key, -1)
    if not raw or #raw > 65536 then return fail('queue_metadata_invalid') end
    local valid, job = pcall(cjson.decode, raw)
    if not valid or type(job) ~= 'table' or type(job.enqueued_at) ~= 'number' then
      return fail('queue_metadata_invalid')
    end
    local t = stamp(job.enqueued_at, false)
    if not t then return fail('queue_metadata_invalid') end
    include(n, t)
  end
end
for _, key in ipairs({'retry', 'schedule'}) do
  local n = redis.call('ZCOUNT', key, '-inf', now)
  if n > 0 then
    local due = redis.call('ZRANGEBYSCORE', key, '-inf', now, 'WITHSCORES', 'LIMIT', 0, 1)
    local t = stamp(due[2], false)
    if not t then return fail('queue_metadata_invalid') end
    include(n, t)
  end
end
return cjson.encode({count=count, oldest=oldest})
"""


def observation(ok: bool, reason: str, **metrics) -> dict:
    return {"ok": ok, "reason": reason, **metrics}


def validate_config(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise TypeError("configuration_error")
    required = {"redis_url", "account_id", "telegram_bot_token", "proxy_url",
                "agent_health_url", "ca_file"}
    optional = {"telegram_alert_token", "telegram_alert_chat_id", "telegram_events"}
    if not required <= raw.keys() or raw.keys() - required - optional:
        raise ValueError("configuration_error")
    config = dict(raw)
    if config["proxy_url"] != PROXY_URL or config["agent_health_url"] != AGENT_URL:
        raise ValueError("configuration_error")
    account = config["account_id"]
    if isinstance(account, bool) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(account)):
        raise ValueError("configuration_error")
    config["account_id"] = str(account)
    for key in ("telegram_bot_token", "telegram_alert_token"):
        if key in config and (not isinstance(config[key], str) or not TOKEN.fullmatch(config[key])):
            raise ValueError("configuration_error")
    redis_url = config["redis_url"]
    if not isinstance(redis_url, str) or len(redis_url) > 4096:
        raise ValueError("configuration_error")
    parsed = urlsplit(redis_url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.fragment:
        raise ValueError("configuration_error")
    ca_file = config["ca_file"]
    if not isinstance(ca_file, str) or not ca_file.startswith("/") or len(ca_file) > 4096:
        raise ValueError("configuration_error")
    events = config.get("telegram_events", [])
    if not isinstance(events, list) or len(events) > 5:
        raise ValueError("configuration_error")
    seen = set()
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"event_id", "text"}
                or not isinstance(event["event_id"], str)
                or not EVENT_ID.fullmatch(event["event_id"]) or event["event_id"] in seen
                or not isinstance(event["text"], str) or not 1 <= len(event["text"]) <= 4096):
            raise ValueError("configuration_error")
        seen.add(event["event_id"])
    if "telegram_alert_chat_id" in config:
        chat = config["telegram_alert_chat_id"]
        if isinstance(chat, bool) or not re.fullmatch(r"-?[1-9][0-9]{0,19}", str(chat)):
            raise ValueError("configuration_error")
        config["telegram_alert_chat_id"] = str(chat)
    if events and not {"telegram_alert_token", "telegram_alert_chat_id"} <= config.keys():
        raise ValueError("configuration_error")
    config["telegram_events"] = events
    return config


def age_observation(stamp: object, now: float, *, max_age: int) -> dict:
    try:
        # Poll stamps are whole Unix seconds; reject permissive float/int conversions.
        value = stamp.decode("ascii") if isinstance(stamp, bytes) else str(stamp)
        if not re.fullmatch(r"[0-9]{1,20}", value):
            raise ValueError
        age = now - int(value)
        if not math.isfinite(age) or age < 0:
            raise ValueError
    except (ValueError, UnicodeError):
        return observation(False, "poll_invalid")
    return observation(age < max_age, "ok" if age < max_age else "poll_stale",
                       age_seconds=round(age, 3))


async def check_poll(redis, config: dict, now: float) -> dict:
    bot_id = TOKEN.fullmatch(config["telegram_bot_token"]).group(1)
    stamp = await redis.get(f"women-help:telegram-ingress:{bot_id}:last_poll")
    return age_observation(stamp, now, max_age=120)


def queue_observation(data: object, now: float) -> dict:
    allowed_errors = {"queue_group_missing", "queue_metadata_invalid"}
    if (isinstance(data, dict) and isinstance(data.get("error"), str)
            and data["error"] in allowed_errors):
        return observation(False, data["error"])
    if not isinstance(data, dict) or set(data) != {"count", "oldest"}:
        return observation(False, "queue_metadata_invalid")
    count, oldest = data["count"], data["oldest"]
    if (type(count) is not int or not 0 <= count <= 2**53
            or type(oldest) not in {int, float} or not math.isfinite(oldest)
            or oldest <= 0 or oldest > now):
        return observation(False, "queue_metadata_invalid")
    age = now - oldest if count else 0
    return observation(age <= 180, "ok" if age <= 180 else "queue_stalled",
                       count=count, oldest_age_seconds=round(age, 3))


async def check_queues(redis, config: dict, now: float) -> dict:
    keys = [f"women-help:chatwoot-events:{config['account_id']}:{lane}" for lane in range(8)]
    projection = await redis.eval(QUEUE_PROJECTION, 8, *keys, now)
    if not isinstance(projection, (str, bytes)) or len(projection) > 4096:
        return observation(False, "queue_metadata_invalid")
    try:
        return queue_observation(json.loads(projection), now)
    except (ValueError, TypeError):
        return observation(False, "queue_metadata_invalid")


async def telegram_request(session, config: dict, token: str, method: str, payload=None):
    # Both URL pieces are validated or internal literals. No redirects or fallback.
    if not TOKEN.fullmatch(token) or method not in {"getMe", "sendMessage"}:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with session.post(url, proxy=config["proxy_url"], json=payload,
                            allow_redirects=False) as response:
        if response.status != 200:
            return None
        try:
            raw = await response.content.readexactly(16385)
        except asyncio.IncompleteReadError as error:
            raw = error.partial
        if len(raw) > 16384:
            return None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            return None
        return result if isinstance(result, dict) and result.get("ok") is True else None


async def check_proxy(session, config: dict) -> dict:
    data = await telegram_request(session, config, config["telegram_bot_token"], "getMe")
    result = data.get("result") if data else None
    expected_id = int(TOKEN.fullmatch(config["telegram_bot_token"]).group(1))
    if (not isinstance(result, dict) or type(result.get("id")) is not int
            or result["id"] != expected_id or result.get("is_bot") is not True):
        return observation(False, "telegram_identity_mismatch")
    return observation(True, "ok")


async def check_agent(session, config: dict) -> dict:
    async with session.get(config["agent_health_url"], allow_redirects=False) as response:
        return observation(response.status == 200,
                           "ok" if response.status == 200 else "agent_unavailable")


async def bounded_check(awaitable, failure: str) -> dict:
    try:
        return await asyncio.wait_for(awaitable, DEADLINE)
    except TimeoutError:
        return observation(False, "probe_timeout")
    except Exception:  # noqa: BLE001 - exception strings may contain credentials
        return observation(False, failure)


async def deliver_event(session, config: dict, event: dict) -> str | None:
    try:
        data = await asyncio.wait_for(telegram_request(
            session, config, config["telegram_alert_token"], "sendMessage",
            {"chat_id": config["telegram_alert_chat_id"], "text": event["text"],
             "disable_web_page_preview": True}), DEADLINE)
        return event["event_id"] if data else None
    except Exception:  # noqa: BLE001 - optional delivery must not affect observations
        return None


async def redis_check(check, config: dict, now: float) -> dict:
    async with Redis.from_url(config["redis_url"], decode_responses=True,
                              socket_timeout=DEADLINE, socket_connect_timeout=DEADLINE,
                              retry_on_timeout=False) as redis:
        return await check(redis, config, now)


async def probe(config: dict) -> dict:
    observations = {}
    delivered = []
    events = config["telegram_events"]

    async def capture(name, awaitable, reason):
        observations[name] = await bounded_check(awaitable, reason)

    async def http_checks():
        # Keep system public roots for Telegram and add the internal agent CA.
        try:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=config["ca_file"])
            connector = aiohttp.TCPConnector(ssl=context)
        except Exception:  # noqa: BLE001 - invalid CA paths must remain private
            observations["telegram_proxy"] = observation(False, "configuration_error")
            observations["agent"] = observation(False, "configuration_error")
            return
        try:
            async with aiohttp.ClientSession(
                    connector=connector, trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=DEADLINE)) as session:
                await asyncio.gather(
                    capture("telegram_proxy", check_proxy(session, config), "telegram_unavailable"),
                    capture("agent", check_agent(session, config), "agent_unavailable"))

                async def deliver(event):
                    event_id = await deliver_event(session, config, event)
                    if event_id is not None:
                        delivered.append(event_id)
                await asyncio.gather(*(deliver(event) for event in events))
        except Exception:  # noqa: BLE001 - HTTP setup must not suppress Redis checks
            observations.setdefault("telegram_proxy", observation(False, "telegram_unavailable"))
            observations.setdefault("agent", observation(False, "agent_unavailable"))

    try:
        async with asyncio.timeout(22):
            now = time.time()
            await asyncio.gather(
                capture("telegram_poll", redis_check(check_poll, config, now), "redis_unavailable"),
                capture("queues", redis_check(check_queues, config, now), "redis_unavailable"),
                http_checks())
    except Exception:  # noqa: BLE001, S110 - no raw exception output is permitted
        pass  # Never print exception text: connection errors routinely contain secrets.
    for name in CHECKS:
        observations.setdefault(name, observation(False, "probe_failed"))
    return {"observations": observations, "telegram_delivered": delivered,
            "telegram_failed": len(delivered) != len(events)}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("configuration_error")
        config = validate_config(json.loads(raw))
        result = asyncio.run(probe(config))
    except Exception:  # noqa: BLE001 - final boundary must never emit credential errors
        result = {"observations": {name: observation(False, "configuration_error") for name in CHECKS},
                  "telegram_delivered": [], "telegram_failed": True}
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

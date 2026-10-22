"""Sequential read-only getMe RTT; run only in the separately bounded container.

The launcher supplies only TELEGRAM_BOT_TOKEN and TELEGRAM_PROXY_URL privately.
First-byte timing means the first response body byte, not a raw HTTP/TLS byte.
"""

import asyncio
import json
import os
import re
import ssl
import time
from types import SimpleNamespace

import aiohttp

CALLS = 10
PROXY = "http://telegram-proxy:3128"
BOT_ID = 8831692160
ERROR_TYPES = {
    "ConfigurationError", "ResponseError", "IdentityError", "TimeoutError", "ClientError",
    "ClientConnectorError", "ClientHttpProxyError", "ClientProxyConnectionError",
    "ClientConnectorCertificateError", "ClientConnectorSSLError", "ClientPayloadError",
    "ServerDisconnectedError", "ClientOSError", "ServerTimeoutError", "ConnectionTimeoutError",
    "SocketTimeoutError",
}


class ConfigurationError(Exception):
    pass


class ResponseError(Exception):
    pass


class IdentityError(Exception):
    pass


def error_type(error):
    name = type(error).__name__
    return name if name in ERROR_TYPES else "ProbeError"


def report(samples):
    return {
        "ok": len(samples) == CALLS and all(row["ok"] for row in samples),
        "read_only": True, "messages_sent": 0, "polling_started": False,
        "expected_identity": len(samples) == CALLS and all(row["ok"] for row in samples),
        "first_byte_definition": "first_response_body_byte",
        "samples": samples,
    }


def trace_config():
    trace = aiohttp.TraceConfig()

    async def connecting(session, context, params):
        context.trace_request_ctx.connect_started = time.perf_counter()

    async def connected(session, context, params):
        attempt = context.trace_request_ctx
        attempt.connect_ms = round((time.perf_counter() - attempt.connect_started) * 1000, 3)

    async def reused(session, context, params):
        context.trace_request_ctx.connection_reused = True

    trace.on_connection_create_start.append(connecting)
    trace.on_connection_create_end.append(connected)
    trace.on_connection_reuseconn.append(reused)
    return trace


async def measure(token, proxy):
    if (not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token)
            or token.partition(":")[0] != str(BOT_ID) or proxy != PROXY):
        raise ConfigurationError
    context = ssl.create_default_context()
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ConfigurationError
    connector = aiohttp.TCPConnector(ssl=context, limit=1, limit_per_host=1)
    samples = []
    async with aiohttp.ClientSession(
        connector=connector, timeout=aiohttp.ClientTimeout(total=15),
        trust_env=False, trace_configs=[trace_config()],
    ) as client:
        for _ in range(CALLS):
            attempt = SimpleNamespace(connect_started=None, connect_ms=None, connection_reused=False)
            started = time.perf_counter()
            first_byte_ms, failure = None, None
            try:
                async with client.post(
                    f"https://api.telegram.org/bot{token}/getMe", proxy=proxy,
                    data=b"", allow_redirects=False, trace_request_ctx=attempt,
                ) as response:
                    first = await response.content.read(1)
                    if first:
                        first_byte_ms = round((time.perf_counter() - started) * 1000, 3)
                    body = first + await response.read()
                    if response.status != 200:
                        raise ResponseError
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise ResponseError from None
                    if not isinstance(payload, dict):
                        raise ResponseError
                    result = payload.get("result")
                    if not (payload.get("ok") is True and isinstance(result, dict)
                            and result.get("id") == BOT_ID and result.get("is_bot") is True):
                        raise IdentityError
            except Exception as error:  # noqa: BLE001 - client errors may contain credentials.
                failure = error_type(error)
            row = {
                "ok": failure is None, "connect_ms": attempt.connect_ms,
                "connection_reused": attempt.connection_reused, "first_byte_ms": first_byte_ms,
                "total_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            if failure:
                row["error_type"] = failure
            samples.append(row)
    return report(samples)


def main():
    try:
        token, proxy = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_PROXY_URL", "")
        value = asyncio.run(measure(token, proxy))
    except Exception as error:  # noqa: BLE001 - never print a credential-bearing traceback.
        value = report([])
        value["error_type"] = error_type(error)
    print("PROXY_PROBE=" + json.dumps(value, allow_nan=False, separators=(",", ":")))
    return 0 if value["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

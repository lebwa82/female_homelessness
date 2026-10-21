"""Real local HTTPS: connection reuse must preserve CA/name checks and identities."""

import socket
import ssl

import aiohttp
import pytest
from aiohttp import web

from app.chatwoot.client import AiohttpChatwootTransport, BinaryAttachment, ChatwootApiError
from tests.test_internal_tls import certificates as _certificates

certificates = _certificates


@pytest.fixture
async def https_endpoint(certificates):
    calls = []

    async def handle(request):
        await request.read()
        calls.append((request.transport, request.headers.get("Api-Access-Token"),
                      request.headers.get("Cookie")))
        response = web.json_response({"ok": True})
        response.set_cookie("session", "synthetic")
        return response

    app = web.Application()
    app.router.add_route("*", "/", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / "chatwoot.crt", certificates / "chatwoot.key")
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"https://127.0.0.1:{port}", calls
    finally:
        await runner.cleanup()


def trust_test_ca(monkeypatch, certificates, *, trusted=True):
    session = aiohttp.ClientSession
    context = (ssl.create_default_context(cafile=str(certificates / "ca.crt"))
               if trusted else ssl.create_default_context())

    class LocalResolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [{"hostname": host, "host": "127.0.0.1", "port": port,
                     "family": socket.AF_INET, "proto": 0, "flags": 0}]

        async def close(self):
            pass

    def create_session(**kwargs):
        # Only the test CA/resolver are injected. Production transport still
        # uses aiohttp's verified TLS defaults, never ssl=False.
        return session(connector=aiohttp.TCPConnector(ssl=context, resolver=LocalResolver()),
                       **kwargs)

    monkeypatch.setattr("app.chatwoot.client.aiohttp.ClientSession", create_session)


@pytest.mark.parametrize("reuse,expected_connections", [(False, 3), (True, 1)])
async def test_json_and_multipart_share_pool_without_cookies_or_identity_leak(
    monkeypatch, certificates, https_endpoint, reuse, expected_connections,
):
    trust_test_ca(monkeypatch, certificates)
    url, calls = https_endpoint
    transport = AiohttpChatwootTransport(url, reuse_connections=reuse)
    try:
        assert await transport.request("GET", "/", "read") == {"ok": True}
        assert await transport.request("POST", "/", "bot", {}) == {"ok": True}
        assert await transport.request_multipart(
            "POST", "/", "bot", {"content": "synthetic"},
            BinaryAttachment("test.pdf", "application/pdf", b"%PDF-synthetic"),
        ) == {"ok": True}
        assert len({connection for connection, _, _ in calls}) == expected_connections
        assert [token for _, token, _ in calls] == ["read", "bot", "bot"]
        assert all(cookie is None for _, _, cookie in calls)
    finally:
        await transport.aclose()
        await transport.aclose()  # Shutdown is safe even after partial initialization.
    assert transport._session is None or transport._session.closed


@pytest.mark.parametrize("wrong_name", [False, True])
async def test_pool_rejects_untrusted_ca_and_wrong_hostname(
    monkeypatch, certificates, https_endpoint, wrong_name,
):
    trust_test_ca(monkeypatch, certificates, trusted=wrong_name)
    url, calls = https_endpoint
    if wrong_name:
        url = url.replace("127.0.0.1", "wrong.invalid")
    transport = AiohttpChatwootTransport(url, reuse_connections=True)
    try:
        with pytest.raises(ChatwootApiError) as failure:
            await transport.request("GET", "/", "read")
        assert isinstance(failure.value.__cause__, aiohttp.ClientConnectorCertificateError)
        assert not calls
    finally:
        await transport.aclose()

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chatwoot import proxy
from app.chatwoot.proxy import upstream_uri


def test_socks_credentials_are_converted_to_library_uri_format():
    assert upstream_uri("socks5://user:pass%40word@proxy.test:1080") == (
        "socks5://proxy.test:1080#user:pass@word"
    )


def test_https_proxy_uses_library_tls_scheme():
    assert upstream_uri("https://proxy.test:443") == "http+ssl://proxy.test:443"


def test_missing_port_raises_without_echoing_secret():
    with pytest.raises(ValueError, match="Unsupported Telegram proxy configuration"):
        upstream_uri("socks5://user:password@proxy.test")


@pytest.mark.asyncio
async def test_runtime_resolves_settings_and_starts_library_server(monkeypatch):
    monkeypatch.setattr(
        proxy,
        "settings",
        SimpleNamespace(resolved_telegram_proxy_url=lambda: "socks5://proxy.test:1080"),
    )
    handler = MagicMock()
    handler.__aenter__ = AsyncMock(return_value=handler)
    handler.__aexit__ = AsyncMock(return_value=False)
    handler.serve_forever = AsyncMock()
    server = SimpleNamespace(start_server=AsyncMock(return_value=handler))
    connection = MagicMock()
    monkeypatch.setattr(proxy.pproxy, "Server", lambda _: server)
    monkeypatch.setattr(proxy.pproxy, "Connection", connection)
    await proxy.serve()
    connection.assert_called_once_with("socks5://proxy.test:1080")
    options = server.start_server.call_args.args[0]
    assert not options["block"]("api.telegram.org")
    assert options["block"]("example.com")

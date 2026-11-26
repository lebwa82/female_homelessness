import asyncio
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


def test_ssh_tunnel_uses_only_the_fixed_private_socket():
    assert upstream_uri("socks5://user:pass%40word@proxy.test:1080", ssh_tunnel=True) == (
        "socks5:///run/nevidimy-proxy/socks.sock#user:pass@word"
    )
    with pytest.raises(ValueError, match="SSH tunnel requires"):
        upstream_uri("http://proxy.test:1080", ssh_tunnel=True)


def test_library_supports_the_authenticated_unix_socket():
    connection = proxy.pproxy.Connection(upstream_uri(
        "socks5://user:pass%40word@proxy.test:1080", ssh_tunnel=True
    ))
    assert connection.unix
    assert connection.bind == "/run/nevidimy-proxy/socks.sock"
    assert connection.auth == b"user:pass@word"


def test_missing_port_raises_without_echoing_secret():
    with pytest.raises(ValueError, match="Unsupported Telegram proxy configuration"):
        upstream_uri("socks5://user:password@proxy.test")


@pytest.mark.asyncio
@pytest.mark.parametrize("use_reserve", [False, True])
async def test_runtime_resolves_settings_and_starts_library_server(monkeypatch, use_reserve):
    monkeypatch.delenv("TELEGRAM_PROXY_RESERVE_URL", raising=False)
    if use_reserve:
        monkeypatch.setenv("TELEGRAM_PROXY_RESERVE_URL", "socks5://backup:pw@reserve.test:1080")
    monkeypatch.setattr(
        proxy,
        "settings",
        SimpleNamespace(resolved_telegram_proxy_url=lambda: "socks5://proxy.test:1080",
                        telegram_proxy_ssh_tunnel=use_reserve),
    )
    handler = MagicMock()
    handler.__aenter__ = AsyncMock(return_value=handler)
    handler.__aexit__ = AsyncMock(return_value=False)
    handler.serve_forever = AsyncMock()
    server = SimpleNamespace(start_server=AsyncMock(return_value=handler))
    monkeypatch.setattr(proxy.pproxy, "Server", lambda _: server)
    await proxy.serve()

    options = server.start_server.call_args.args[0]
    (remote,) = options["rserver"]
    assert not remote.direct
    if use_reserve:
        assert remote.connection.bind == "/run/nevidimy-proxy/socks.sock"
        assert remote.reserve.bind == "/run/nevidimy-proxy/reserve-socks.sock"
        assert remote.reserve.auth == b"backup:pw"
    else:
        assert remote.connection.bind == "proxy.test:1080"
        assert remote.reserve is None
    assert not options["block"]("api.telegram.org")
    assert options["block"]("example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_is_ssh,reserve_scheme", [(False, "socks5"), (True, "http")])
async def test_reserve_requires_ssh_and_socks5(monkeypatch, primary_is_ssh, reserve_scheme):
    monkeypatch.setenv(
        "TELEGRAM_PROXY_RESERVE_URL", f"{reserve_scheme}://user:reserve-secret@reserve.test:1080"
    )
    monkeypatch.setattr(
        proxy,
        "settings",
        SimpleNamespace(resolved_telegram_proxy_url=lambda: "socks5://proxy.test:1080",
                        telegram_proxy_ssh_tunnel=primary_is_ssh),
    )
    with pytest.raises(ValueError) as error:
        await proxy.serve()
    assert "reserve-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_tunnel_failure_closes_socket_and_retries_before_connect_ack(monkeypatch, caplog):
    first, second = MagicMock(), MagicMock()
    reader = object()
    remote = SimpleNamespace(
        open_connection=AsyncMock(side_effect=[(reader, first), (reader, second)]),
        prepare_connection=AsyncMock(side_effect=[TimeoutError("private-proxy-password"), (reader, second)]),
        direct=False,
    )
    sleep = AsyncMock()
    monkeypatch.setattr(proxy.asyncio, "sleep", sleep)
    upstream = proxy.TelegramUpstream(remote)
    assert await upstream.open_connection("api.telegram.org", 443, None, None) == (reader, second)
    first.close.assert_called_once()
    second.close.assert_not_called()
    assert remote.open_connection.await_count == 2
    assert not upstream.direct
    assert "private-proxy-password" not in caplog.text
    assert await upstream.prepare_connection(reader, second, "api.telegram.org", 443) == (reader, second)
    assert remote.prepare_connection.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_fails", [False, True])
async def test_reserve_is_used_only_after_failed_primary_negotiation(primary_fails, caplog):
    first, second = (object(), MagicMock()), (object(), MagicMock())
    primary = SimpleNamespace(
        open_connection=AsyncMock(return_value=first),
        prepare_connection=AsyncMock(return_value=first),
    )
    if primary_fails:
        primary.prepare_connection.side_effect = AssertionError("private-primary-password")
    reserve = SimpleNamespace(
        open_connection=AsyncMock(return_value=second),
        prepare_connection=AsyncMock(return_value=second),
    )
    upstream = proxy.TelegramUpstream(primary, reserve)
    target = ("api.telegram.org", 443)
    connected = await upstream.open_connection(*target, None, None)
    assert connected == (second if primary_fails else first)
    primary.open_connection.assert_awaited_once_with(*target, None, None, timeout=3)
    primary.prepare_connection.assert_awaited_once_with(*first, *target)
    if primary_fails:
        first[1].close.assert_called_once()
        reserve.open_connection.assert_awaited_once_with(*target, None, None, timeout=5)
        reserve.prepare_connection.assert_awaited_once_with(*second, *target)
        second[1].close.assert_not_called()
    else:
        first[1].close.assert_not_called()
        reserve.open_connection.assert_not_awaited()
    assert "private-primary-password" not in caplog.text


@pytest.mark.asyncio
async def test_both_routes_failing_are_not_retried_or_replaced_by_direct():
    writer = MagicMock()
    primary = SimpleNamespace(open_connection=AsyncMock(side_effect=OSError()), direct=False)
    reserve = SimpleNamespace(
        open_connection=AsyncMock(return_value=(object(), writer)),
        prepare_connection=AsyncMock(side_effect=EOFError()),
    )
    upstream = proxy.TelegramUpstream(primary, reserve)
    with pytest.raises(EOFError):
        await upstream.open_connection("api.telegram.org", 443, None, None)
    primary.open_connection.assert_awaited_once()
    reserve.open_connection.assert_awaited_once()
    writer.close.assert_called_once()
    assert not upstream.direct


@pytest.mark.asyncio
async def test_proxy_tunnel_retries_are_bounded(monkeypatch):
    writer = MagicMock()
    remote = SimpleNamespace(
        open_connection=AsyncMock(return_value=(object(), writer)),
        prepare_connection=AsyncMock(side_effect=EOFError()),
    )
    monkeypatch.setattr(proxy.asyncio, "sleep", AsyncMock())
    with pytest.raises(EOFError):
        await proxy.TelegramUpstream(remote).open_connection("api.telegram.org", 443, None, None)
    assert remote.open_connection.await_count == 3
    assert writer.close.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("use_reserve", [False, True])
async def test_cancelled_proxy_tunnel_is_closed_without_retry(use_reserve):
    writer = MagicMock()
    remote = SimpleNamespace(
        open_connection=AsyncMock(return_value=(object(), writer)),
        prepare_connection=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    reserve = SimpleNamespace(open_connection=AsyncMock()) if use_reserve else None
    upstream = proxy.TelegramUpstream(remote, reserve)
    with pytest.raises(asyncio.CancelledError):
        await upstream.open_connection("api.telegram.org", 443, None, None)
    writer.close.assert_called_once()
    assert remote.open_connection.await_count == 1
    if reserve:
        reserve.open_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_stalled_socks_negotiation_is_timed_out(monkeypatch):
    writer = MagicMock()

    async def stalled(*args):
        await asyncio.sleep(100)

    remote = SimpleNamespace(
        open_connection=AsyncMock(return_value=(object(), writer)),
        prepare_connection=stalled,
    )
    original_timeout = asyncio.timeout
    monkeypatch.setattr(proxy.asyncio, "timeout", lambda _: original_timeout(0.01))
    # Do not replace asyncio.sleep: it is also the operation being timed out.
    with pytest.raises(TimeoutError):
        await proxy.TelegramUpstream(remote).open_connection("api.telegram.org", 443, None, None)
    assert writer.close.call_count == 3

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from deploy.timeweb import proxy_tunnel_ready as tunnel


@pytest.mark.parametrize("chunks, expected", [
    ([b"\x05", b"\x02"], True),
    ([b"\x05", b""], False),
    ([b"\x05\xff"], False),
])
def test_readiness_requires_a_complete_socks_auth_reply(monkeypatch, chunks, expected):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.side_effect = chunks
    monkeypatch.setattr(tunnel.socket, "socket", lambda *args: connection)
    assert tunnel.ready() is expected
    connection.connect.assert_called_once_with(tunnel.SOCKET_PATH)
    connection.settimeout.assert_called_once_with(2)
    connection.sendall.assert_called_once_with(b"\x05\x01\x02")


def test_socket_failure_is_not_ready(monkeypatch):
    def fail(*args):
        raise OSError("unavailable")
    monkeypatch.setattr(tunnel.socket, "socket", fail)
    assert not tunnel.ready()


def test_readiness_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(tunnel.time, "monotonic", MagicMock(side_effect=[0, 1, 21]))
    monkeypatch.setattr(tunnel.time, "sleep", MagicMock())
    monkeypatch.setattr(tunnel, "ready", lambda: False)
    assert not tunnel.wait_ready()
    tunnel.time.sleep.assert_called_once_with(0.25)


def test_tunnel_reconnect_does_not_stop_the_telegram_consumer():
    unit = (Path(__file__).parent.parent / "deploy/timeweb/nevidimy-telegram.service").read_text()
    lines = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    assert "nevidimy-proxy-tunnel.service" in lines["Wants"].split()
    assert "nevidimy-proxy-tunnel.service" not in lines["Requires"].split()
    assert "nevidimy-proxy-tunnel.service" in lines["After"].split()

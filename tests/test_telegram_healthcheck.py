import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chatwoot import telegram_healthcheck


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp,expected", [(None, False), (b"990", True), (b"880", False), (b"1001", False)])
async def test_probe_reads_only_the_scoped_heartbeat(monkeypatch, stamp, expected):
    import app.config

    monkeypatch.setattr(app.config, "settings", SimpleNamespace(
        telegram_bot_token="12345678:test-only-not-a-real-token", telegram_ingress_redis_url="redis://test-only/0"))
    monkeypatch.setattr(telegram_healthcheck.time, "time", lambda: 1000)
    redis = MagicMock()
    redis.get = AsyncMock(return_value=stamp)
    redis.__aenter__ = AsyncMock(return_value=redis)
    redis.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=redis)
    monkeypatch.setattr(telegram_healthcheck.Redis, "from_url", factory)

    assert await telegram_healthcheck.healthy() is expected

    redis.get.assert_awaited_once_with("women-help:telegram-ingress:12345678:last_poll")
    factory.assert_called_once_with("redis://test-only/0", socket_timeout=5, socket_connect_timeout=5)


def test_import_does_not_load_telegram_sdk():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import app.chatwoot.telegram_healthcheck; assert 'aiogram' not in sys.modules"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr

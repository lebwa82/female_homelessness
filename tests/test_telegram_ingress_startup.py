import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe

from app.chatwoot import telegram_ingress


@pytest.mark.asyncio
async def test_transient_preparation_failure_retries_without_restarting_sdk(monkeypatch):
    ingress = SimpleNamespace(prepare=AsyncMock(side_effect=[TimeoutError(), TimeoutError(), None]))
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_ingress.asyncio, "sleep", sleep)

    await telegram_ingress.prepare_until_ready(ingress)

    assert ingress.prepare.await_count == 3
    assert sleep.await_args_list == [call(1), call(2)]


@pytest.mark.asyncio
async def test_invalid_credentials_are_not_retried(monkeypatch):
    error = TelegramUnauthorizedError(method=GetMe(), message="test-only authentication failure")
    ingress = SimpleNamespace(prepare=AsyncMock(side_effect=error))
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_ingress.asyncio, "sleep", sleep)

    with pytest.raises(TelegramUnauthorizedError):
        await telegram_ingress.prepare_until_ready(ingress)

    assert ingress.prepare.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_preparation_cancellation_is_not_swallowed():
    ingress = SimpleNamespace(prepare=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await telegram_ingress.prepare_until_ready(ingress)

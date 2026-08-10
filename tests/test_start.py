from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot
from app.service import MAIN_OPTIONS


@pytest.mark.asyncio
async def test_start_records_safe_welcome_and_shows_main_options(monkeypatch: pytest.MonkeyPatch) -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=101), chat=SimpleNamespace(id=202), message_id=303)
    conversation = SimpleNamespace(id=404)
    get_or_create = AsyncMock(return_value=conversation)
    record_event = AsyncMock()
    record_message = AsyncMock()
    reply_and_store = AsyncMock()
    monkeypatch.setattr(bot, "get_or_create_conversation", get_or_create)
    monkeypatch.setattr(bot, "record_event", record_event)
    monkeypatch.setattr(bot, "record_message", record_message)
    monkeypatch.setattr(bot, "reply_and_store", reply_and_store)

    await bot.start(message)

    get_or_create.assert_awaited_once_with(101)
    record_event.assert_awaited_once_with(404, "started")
    record_message.assert_awaited_once_with(404, "user", "/start")
    _, _, text = reply_and_store.await_args.args
    assert "не является экстренной службой" in text
    assert "не даёт полной анонимности" in text
    assert "Можно не называть имя" in text
    assert reply_and_store.await_args.kwargs["buttons"] == MAIN_OPTIONS

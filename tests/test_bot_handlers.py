import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot
from app.domain import AgentTurn, Choice


def message() -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=101, username="tester"),
        chat=SimpleNamespace(id=202),
        message_id=303,
        date=SimpleNamespace(isoformat=lambda: "2026-08-20T10:00:00+00:00"),
        text="мне нужна еда",
    )


@pytest.mark.asyncio
async def test_text_handler_passes_free_text_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(handle_text=AsyncMock(return_value=AgentTurn(text="Что сейчас важнее?")))
    send_turn = AsyncMock()
    monkeypatch.setattr(bot, "conversation_service", service)
    monkeypatch.setattr(bot, "send_turn", send_turn)

    await bot.reply(message())

    assert service.handle_text.await_args.args[0].text == "мне нужна еда"
    send_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_handler_answers_callback_and_keeps_bot_available(monkeypatch: pytest.MonkeyPatch) -> None:
    message_value = message()
    callback = SimpleNamespace(
        data="human",
        from_user=message_value.from_user,
        message=message_value,
        answer=AsyncMock(),
    )
    turn = AgentTurn(text="Зову человека", choices=(Choice(id="continue_bot", label="Продолжить здесь"),))
    service = SimpleNamespace(handle_callback=AsyncMock(return_value=turn))
    send_turn = AsyncMock()
    monkeypatch.setattr(bot, "conversation_service", service)
    monkeypatch.setattr(bot, "send_turn", send_turn)

    await bot.callback(callback)

    callback.answer.assert_awaited_once()
    assert service.handle_callback.await_args.args[1] == "human"
    assert send_turn.await_args.args[2].choices[0].id == "continue_bot"


@pytest.mark.asyncio
async def test_media_handler_uses_text_only_fallback_with_human_button(monkeypatch: pytest.MonkeyPatch) -> None:
    value = message()
    value.text = None
    send_turn = AsyncMock()
    service = SimpleNamespace(claim_inbound=AsyncMock(return_value=True))
    monkeypatch.setattr(bot, "send_turn", send_turn)
    monkeypatch.setattr(bot, "conversation_service", service)

    await bot.unsupported_content(value)

    turn = send_turn.await_args.args[2]
    assert "текстом" in turn.text.lower()
    assert any(choice.id == "human" for choice in turn.choices)


@pytest.mark.asyncio
async def test_stateless_redelivery_is_not_rendered_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(claim_inbound=AsyncMock(return_value=False))
    send_turn = AsyncMock()
    monkeypatch.setattr(bot, "conversation_service", service)
    monkeypatch.setattr(bot, "send_turn", send_turn)

    await bot.system_info(message())
    await bot.unsupported_content(message())

    assert send_turn.await_count == 0


@pytest.mark.asyncio
async def test_polling_lifecycle_starts_and_cancels_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_started = asyncio.Event()

    async def worker(_bot) -> None:  # type: ignore[no-untyped-def]
        worker_started.set()
        await asyncio.Event().wait()

    async def polling(_bot) -> None:  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)

    fake_bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(bot, "worker_loop", worker)
    monkeypatch.setattr(bot.dp, "start_polling", polling)

    await bot.poll_once(fake_bot)

    assert worker_started.is_set()
    fake_bot.session.close.assert_awaited_once()

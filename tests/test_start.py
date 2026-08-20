from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot
from app.domain import AgentTurn, Choice


@pytest.mark.asyncio
async def test_start_uses_service_welcome_without_runtime_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=101, username="tester"),
        chat=SimpleNamespace(id=202),
        message_id=303,
        date=SimpleNamespace(isoformat=lambda: "2026-08-20T10:00:00+00:00"),
    )
    turn = AgentTurn(text="Привет. Хотите продолжить?", choices=(Choice(id="continue", label="Да"),))
    service = SimpleNamespace(start=AsyncMock(return_value=turn))
    send_turn = AsyncMock()
    monkeypatch.setattr(bot, "conversation_service", service)
    monkeypatch.setattr(bot, "send_turn", send_turn)

    await bot.start(message)

    service.start.assert_awaited_once()
    assert "тест" not in send_turn.await_args.args[2].text.lower()
    assert send_turn.await_args.args[2] == turn


@pytest.mark.asyncio
async def test_system_info_stays_hidden_and_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=101, username="tester"),
        chat=SimpleNamespace(id=202),
        message_id=303,
        date=SimpleNamespace(isoformat=lambda: "2026-08-20T10:00:00+00:00"),
    )
    send_turn = AsyncMock()
    monkeypatch.setattr(bot, "send_turn", send_turn)
    monkeypatch.setattr(bot.settings, "app_env", "production")
    monkeypatch.setattr(bot.settings, "build_version", "abc1234")
    monkeypatch.setattr(bot.settings, "llm_enabled", True)

    await bot.system_info(message)

    text = send_turn.await_args.args[2].text
    assert "ENV: production" in text
    assert "Сборка: abc1234" in text
    assert "YANDEX" not in text
    assert "TOKEN" not in text

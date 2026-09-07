from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Update

from app.chatwoot.telegram_ingress import ChatwootUnavailable, Ingress, error_kinds, keep_lease
from deploy.chatwoot import activate


def update(number=101, *, callback=False):
    sender = {"id": 9001, "is_bot": False, "first_name": "Test"}
    message = {"message_id": 7, "from": sender, "chat": {"id": 9001, "type": "private"},
               "date": 1788742800, "text": "/system_info"}
    payload = {"update_id": number}
    if callback:
        payload["callback_query"] = {"id": "test-click", "from": sender,
                                     "chat_instance": "test", "message": message, "data": "back"}
    else:
        payload["message"] = message
    return Update.model_validate(payload)


class MemoryRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value


class Http:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.error = None

    @asynccontextmanager
    async def get(self, url, **kwargs):
        assert url == "http://chatwoot:3000/"
        yield SimpleNamespace(status=self.status)

    @asynccontextmanager
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        yield SimpleNamespace(status=self.status)


def setup(redis=None, *, callback=False):
    bot = SimpleNamespace(id=42, token="42:fixture", get_me=AsyncMock(),
                          delete_webhook=AsyncMock(), answer_callback_query=AsyncMock(),
                          get_updates=AsyncMock(return_value=[update(callback=callback)]))
    http = Http()
    return Ingress(bot, http, redis or MemoryRedis(), "http://chatwoot:3000"), bot, http


async def test_prepare_preserves_pending_updates():
    ingress, bot, _ = setup()
    await ingress.prepare()
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False, request_timeout=15)


async def test_unready_receiver_does_not_disable_webhook():
    ingress, bot, http = setup()
    http.status = 503
    with pytest.raises(ChatwootUnavailable):
        await ingress.prepare()
    bot.delete_webhook.assert_not_awaited()


async def test_native_payload_and_restart_uses_persisted_cursor():
    redis = MemoryRedis()
    ingress, _, http = setup(redis)
    await ingress.poll_once()
    request = http.calls[0][1]
    message = request["json"]["telegram"]["message"]
    assert message["from"]["id"] == 9001
    assert int(message["date"]) == 1788742800
    assert request["allow_redirects"] is False
    assert redis.data[f"{ingress.prefix}:offset"] == 102
    restarted, second_bot, second_http = setup(redis)
    second_bot.get_updates.return_value = []
    await restarted.poll_once()
    assert second_bot.get_updates.call_args.kwargs["offset"] == 102
    assert second_http.calls == []


@pytest.mark.parametrize("status", [301, 302, 401, 404, 500, 503])
async def test_no_acknowledgement_on_failed_forward(status):
    ingress, _, http = setup()
    http.status = status
    with pytest.raises(ChatwootUnavailable):
        await ingress.poll_once()
    assert f"{ingress.prefix}:offset" not in ingress.redis.data
    http.status = 200
    await ingress.poll_once()
    assert ingress.redis.data[f"{ingress.prefix}:offset"] == 102


async def test_no_acknowledgement_on_timeout():
    ingress, _, http = setup()
    http.error = TimeoutError()
    with pytest.raises(TimeoutError):
        await ingress.poll_once()
    assert f"{ingress.prefix}:offset" not in ingress.redis.data


async def test_click_payload_is_forwarded_and_spinner_acknowledged():
    ingress, bot, http = setup(callback=True)
    await ingress.poll_once()
    callback = http.calls[0][1]["json"]["telegram"]["callback_query"]
    assert callback["data"] == "back"
    assert callback["from"]["id"] == 9001
    bot.answer_callback_query.assert_awaited_once_with("test-click", request_timeout=10)


async def test_expired_callback_does_not_replay_click(caplog):
    ingress, bot, _ = setup(callback=True)
    bot.answer_callback_query.side_effect = TelegramBadRequest(
        method=AnswerCallbackQuery(callback_query_id="fixture"), message="private exception content"
    )
    await ingress.poll_once()
    assert ingress.redis.data[f"{ingress.prefix}:offset"] == 102
    assert "private exception content" not in caplog.text


async def test_lease_loss_propagates_and_stops_owner(monkeypatch):
    monkeypatch.setattr("app.chatwoot.telegram_ingress.asyncio.sleep", AsyncMock())
    lock = SimpleNamespace(extend=AsyncMock(side_effect=RuntimeError("lease lost")))
    with pytest.raises(RuntimeError):
        await keep_lease(lock)


def test_nested_failures_log_classes_only():
    error = ExceptionGroup("private payload", [
        ExceptionGroup("private URL", [ChatwootUnavailable("private credentials")]),
        TimeoutError("private token"),
    ])
    assert error_kinds(error) == "ChatwootUnavailable,TimeoutError"


def test_ingress_env_contains_only_transport_credentials(tmp_path, monkeypatch, capsys):
    bot, agent, cw, ingress = [tmp_path / name for name in ("bot", "agent", "cw", "ingress")]
    bot.write_text("TELEGRAM_BOT_TOKEN=42:fixture\nYANDEX_AI_API_KEY=never-copy\n")
    agent.write_text("TELEGRAM_PROXY_URL=socks5://user:private@localhost:1080\nCHATWOOT_READ_TOKEN=no\n")
    cw.write_text("TELEGRAM_UPDATE_TRANSPORT=polling\n")
    for key, path in [("BOT", bot), ("AGENT", agent), ("CW", cw), ("INGRESS", ingress)]:
        monkeypatch.setattr(activate, key, path)
    activate.prepare_ingress()
    values = activate.env(ingress)
    assert set(values) == {"TELEGRAM_UPDATE_TRANSPORT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_PROXY_URL",
                           "CHATWOOT_BASE_URL", "TELEGRAM_INGRESS_REDIS_URL"}
    assert ingress.stat().st_mode & 0o777 == 0o600
    assert "private" not in capsys.readouterr().out

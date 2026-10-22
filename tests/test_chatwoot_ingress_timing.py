import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aiogram.types import Update
from test_telegram_ingress import setup, update

from app.chatwoot.telegram_ingress import ChatwootUnavailable

ROOT = Path(__file__).resolve().parents[1]
FIELD = "nevidimy_received_at_ms"
BASE_MS = 1_700_000_000_123


async def test_received_timestamp_precedes_callback_ack_and_never_changes_user_payload(monkeypatch):
    ingress, bot, http = setup(callback=True)
    original = bot.get_updates.return_value[0].model_dump()
    clock = [BASE_MS * 1_000_000]
    monkeypatch.setattr("app.chatwoot.telegram_ingress.time.time_ns", lambda: clock[0])

    async def ack(*args, **kwargs):
        assert ingress._received_at_ms == BASE_MS
        assert http.calls == []
        clock[0] += 2_000_000_000

    bot.answer_callback_query.side_effect = ack
    await ingress.poll_once()
    raw = http.calls[0][1]["json"]["telegram"]
    assert raw[FIELD] == BASE_MS
    assert FIELD not in raw["callback_query"]
    assert FIELD not in raw["callback_query"]["message"]
    assert raw["callback_query"]["data"] == original["callback_query"]["data"]
    assert raw["callback_query"]["message"]["text"] == original["callback_query"]["message"]["text"]
    assert bot.get_updates.return_value[0].model_dump() == original
    assert ingress._received_at_ms is None
    assert ingress.redis.data[f"{ingress.prefix}:offset"] == 102


async def test_local_timestamp_overrides_untrusted_extra_without_mutating_update(monkeypatch):
    ingress, bot, http = setup()
    source = Update.model_validate({**update().model_dump(exclude_none=True), FIELD: 1})
    bot.get_updates.return_value = [source]
    monkeypatch.setattr("app.chatwoot.telegram_ingress.time.time_ns", lambda: BASE_MS * 1_000_000)
    await ingress.poll_once()
    raw = http.calls[0][1]["json"]["telegram"]
    assert raw[FIELD] == BASE_MS
    assert source.model_dump()[FIELD] == 1
    assert FIELD not in raw["message"]


async def test_failed_forward_clears_timestamp_without_advancing_cursor(monkeypatch):
    ingress, _, http = setup(callback=True)
    clock = [BASE_MS * 1_000_000]
    monkeypatch.setattr("app.chatwoot.telegram_ingress.time.time_ns", lambda: clock[0])
    http.status = 503
    with pytest.raises(ChatwootUnavailable):
        await ingress.poll_once()
    assert ingress._received_at_ms is None
    assert f"{ingress.prefix}:offset" not in ingress.redis.data
    clock[0] += 5_000_000_000
    http.status = 200
    await ingress.forward(update())
    assert http.calls[-1][1]["json"]["telegram"][FIELD] == BASE_MS + 5000


@pytest.mark.parametrize("first_completed", ["ack", "forward"])
async def test_callback_ack_overlaps_forward_and_cursor_waits_for_both(monkeypatch, first_completed):
    ingress, bot, _ = setup(callback=True)
    ack_started, forward_started = asyncio.Event(), asyncio.Event()
    ack_release, forward_release = asyncio.Event(), asyncio.Event()
    ack_completed, forward_completed = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr("app.chatwoot.telegram_ingress.time.time_ns", lambda: BASE_MS * 1_000_000)

    async def ack(*args, **kwargs):
        assert ingress._received_at_ms == BASE_MS
        ack_started.set()
        await ack_release.wait()
        ack_completed.set()

    async def forward(received):
        assert ingress._received_at_ms == BASE_MS
        assert ack_started.is_set()
        forward_started.set()
        await forward_release.wait()
        forward_completed.set()

    bot.answer_callback_query.side_effect = ack
    monkeypatch.setattr(ingress, "forward", forward)
    polling = asyncio.create_task(ingress.poll_once())
    try:
        await asyncio.wait_for(forward_started.wait(), timeout=1)
        assert ack_started.is_set() and not ack_completed.is_set()
        assert f"{ingress.prefix}:offset" not in ingress.redis.data
        if first_completed == "ack":
            ack_release.set()
            await asyncio.wait_for(ack_completed.wait(), timeout=1)
            assert not forward_completed.is_set()
        else:
            forward_release.set()
            await asyncio.wait_for(forward_completed.wait(), timeout=1)
            assert not ack_completed.is_set()
        assert not polling.done()
        assert f"{ingress.prefix}:offset" not in ingress.redis.data
        ack_release.set()
        forward_release.set()
        await asyncio.wait_for(polling, timeout=1)
    finally:
        if not polling.done():
            polling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await polling
    assert ingress.redis.data[f"{ingress.prefix}:offset"] == 102
    assert ingress._received_at_ms is None


async def test_forward_failure_cancels_and_awaits_blocked_ack_without_hiding_error(monkeypatch):
    ingress, bot, _ = setup(callback=True)
    ack_started, ack_finished = asyncio.Event(), asyncio.Event()
    ack_tasks = []

    async def ack(*args, **kwargs):
        ack_tasks.append(asyncio.current_task())
        ack_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ack_finished.set()

    async def forward(received):
        await ack_started.wait()
        raise ChatwootUnavailable("synthetic native failure")

    bot.answer_callback_query.side_effect = ack
    monkeypatch.setattr(ingress, "forward", forward)
    with pytest.raises(ChatwootUnavailable, match="synthetic native failure"):
        await asyncio.wait_for(ingress.poll_once(), timeout=1)
    assert ack_finished.is_set() and ack_tasks[0].cancelled()
    assert f"{ingress.prefix}:offset" not in ingress.redis.data
    assert ingress._received_at_ms is None


@pytest.mark.parametrize("forward_accepted", [False, True])
async def test_poll_cancellation_leaves_no_ack_or_forward_task_and_no_offset(monkeypatch, forward_accepted):
    ingress, bot, _ = setup(callback=True)
    ack_started, ack_finished = asyncio.Event(), asyncio.Event()
    forward_started, forward_finished = asyncio.Event(), asyncio.Event()
    tasks = []

    async def ack(*args, **kwargs):
        tasks.append(asyncio.current_task())
        ack_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ack_finished.set()

    async def forward(received):
        tasks.append(asyncio.current_task())
        forward_started.set()
        try:
            if not forward_accepted:
                await asyncio.Event().wait()
        finally:
            forward_finished.set()

    bot.answer_callback_query.side_effect = ack
    monkeypatch.setattr(ingress, "forward", forward)
    polling = asyncio.create_task(ingress.poll_once())
    await asyncio.wait_for(ack_started.wait(), timeout=1)
    await asyncio.wait_for(forward_started.wait(), timeout=1)
    if forward_accepted:
        await asyncio.wait_for(forward_finished.wait(), timeout=1)
    polling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await polling
    assert ack_finished.is_set() and forward_finished.is_set()
    assert tasks and all(task.done() for task in tasks)
    assert f"{ingress.prefix}:offset" not in ingress.redis.data
    assert ingress._received_at_ms is None


def callback_outputs(fixtures):
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for native timestamp propagation")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_telegram_callback_probe.rb")],
        input=json.dumps(fixtures),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_native_timestamp_propagation_preserves_callback_routing_and_unknown_bots():
    params = {
        FIELD: BASE_MS,
        "callback_query": {
            "data": "synthetic-action",
            "message": {"reply_markup": {"inline_keyboard": [[
                {"text": "synthetic label", "callback_data": "synthetic-action"},
            ]]}},
        },
    }
    fixtures = [{"bot_name": name, "params": params} for name in (
        "nevidima_bot", "nevidima_bot_mvp_bot", "unrelated_bot",
    )]
    first, second, unknown = callback_outputs(fixtures)
    for approved in (first, second):
        assert approved["content"] == "Нажата кнопка: «synthetic label»."
        assert approved["content_attributes"] == {
            "in_reply_to_external_id": 19,
            "nevidimy_telegram_callback_data": "synthetic-action",
            FIELD: BASE_MS,
        }
    assert unknown == {
        "content": "synthetic-action", "content_attributes": {"in_reply_to_external_id": 19},
    }


@pytest.mark.parametrize("received_at", [None, True, 0, -1, 1, 1.5, "1700000000123", 10**20])
def test_native_timing_rejects_invalid_values_without_user_text_injection(received_at):
    ordinary, = callback_outputs([{
        "bot_name": "nevidima_bot",
        "params": {FIELD: received_at, "message": {"text": "synthetic text", FIELD: BASE_MS}},
    }])
    assert ordinary == {
        "content": "synthetic text", "content_attributes": {"in_reply_to_external_id": 19},
    }


def test_sender_telemetry_has_honest_boundaries_and_never_logs_content_or_errors():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for native sender telemetry")
    probe = r'''
require 'json'
require 'ostruct'
class ProbeLog
  attr_accessor :fail
  attr_reader :lines
  def initialize; @lines = []; end
  def info(value)
    raise IOError, 'private logger error' if fail
    @lines << value
  end
end
module Rails
  def self.logger; @logger ||= ProbeLog.new; end
  def self.application
    OpenStruct.new(reloader: Object.new.tap do |value|
      def value.to_prepare(&block); block.call; end
    end)
  end
end
module HTTParty; end
module Channel
  class Telegram
    attr_accessor :mode, :bot_name
    attr_reader :calls
    def initialize; @bot_name = 'nevidima_bot'; @calls = 0; end
    def send_message_on_telegram(message)
      @calls += 1
      message_request
      raise IOError, 'private native token URL chat_id text' if mode == :raise
      return nil if mode == :nil
      return 0 if mode == :zero
      return '' if mode == :empty
      return '42' if mode == :string
      37
    end
    private
    def message_request; :native_http; end
  end
end
require ARGV.fetch(0)
module NevidimyTelegramTransport
  def self.monotonic; @clock = (@clock || 0) + 0.01; end
  def self.wall_ms; 1_700_000_000_373; end
end
def verify(value, reason); raise reason unless value; end
message = OpenStruct.new(id: 123, content: 'private content',
                         content_attributes: {'nevidimy_received_at_ms' => 1_700_000_000_123,
                                              'unused' => 'private callback chat_id token'})
channel = Channel::Telegram.new
verify(channel.public_methods.include?(:send_message_on_telegram), 'sender visibility changed')
verify(channel.send_message_on_telegram(message) == 37, 'native numeric ID changed')
accepted = JSON.parse(Rails.logger.lines.last)
keys = %w[event message_id http_elapsed_ms pool_checkout_ms connection_reused tls_setup_ms send_elapsed_ms accepted_at_ms source_to_accepted_ms success outcome_class]
verify(accepted.keys.sort == keys.sort, 'telemetry fields are not allowlisted')
verify(accepted['message_id'] == 123 && accepted['http_elapsed_ms'] == 10 && accepted['send_elapsed_ms'] == 30, 'wrong monotonic boundaries')
verify(accepted['accepted_at_ms'] == 1_700_000_000_373 && accepted['source_to_accepted_ms'] == 250, 'wrong wall-clock boundary')
verify(accepted['success'] == true && accepted['outcome_class'] == 'Integer', 'numeric acceptance not recorded')
channel.mode = :string
verify(channel.send_message_on_telegram(message) == '42', 'native string ID changed')
verify(JSON.parse(Rails.logger.lines.last)['success'] == true, 'string acceptance not recorded')
[:nil, :zero, :empty].each do |mode|
  channel.mode = mode
  channel.send_message_on_telegram(message)
  failed = JSON.parse(Rails.logger.lines.last)
  verify(!failed['success'] && failed['accepted_at_ms'].nil? && failed['source_to_accepted_ms'].nil?, 'failed native return was labeled accepted')
end
channel.mode = :raise
begin
  channel.send_message_on_telegram(message)
  raise 'native failure was swallowed'
rescue IOError => error
  verify(error.message == 'private native token URL chat_id text', 'native error changed')
end
failure = JSON.parse(Rails.logger.lines.last)
verify(failure['outcome_class'] == 'IOError' && !failure['success'], 'failure class missing')
verify(Thread.current[NevidimyTelegramTransport::TIMING_KEY].nil?, 'timing scope leaked')
verify(!Rails.logger.lines.join.match?(/private|chat_id|token|https?:\/\/|api\.telegram/), 'telemetry leaked content')
before = Rails.logger.lines.size
channel.bot_name = 'unrelated_bot'
channel.mode = nil
verify(channel.send_message_on_telegram(message) == 37 && Rails.logger.lines.size == before, 'unknown bot behavior changed')
channel.bot_name = 'nevidima_bot'
message.content_attributes['nevidimy_received_at_ms'] = 1_800_000_000_000
channel.send_message_on_telegram(message)
verify(JSON.parse(Rails.logger.lines.last)['source_to_accepted_ms'].nil?, 'future timestamp was clamped into a claimed duration')
Rails.logger.fail = true
verify(channel.send_message_on_telegram(message) == 37, 'logging failure changed native delivery')
puts JSON.generate(verified: true)
'''
    result = subprocess.run(
        [ruby, "-e", probe, str(ROOT / "deploy/timeweb/telegram_transport.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"verified": True}

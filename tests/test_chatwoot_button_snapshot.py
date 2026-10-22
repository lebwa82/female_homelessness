"""Neutral contracts for signed, speculative snapshots; never delivery authority."""

import json
import time
from copy import deepcopy
from dataclasses import asdict, make_dataclass, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chatwoot.app import AgentBotWebhook
from app.chatwoot.contracts import IncomingChatwootMessage, parse_message_created
from app.chatwoot.event_queue import DurableEventQueue
from app.chatwoot.fast_buttons import CAPABILITY, PRIVATE_NOTES_FEATURE
from app.chatwoot.telemetry import event_timing
from tests.test_chatwoot_app import FakeRequest, RecordingService, _signed_request
from tests.test_chatwoot_service import event

pytest_plugins = ("tests.test_chatwoot_fast_buttons", "tests.test_chatwoot_event_queue")

# Frozen consumer from 7b85521 (verified against the pre-edit local source).
# Keep its old, strict five-field constructor: no .runtime or Git CI dependency.
EVENTS = {"IncomingChatwootMessage": make_dataclass("IncomingChatwootMessage", [
    ("message_id", int), ("conversation_id", int), ("contact_id", int),
    ("inbox_id", int), ("content", str),
])}


class LegacyEventQueue(DurableEventQueue):
    async def consume_one(self, key: str, *, block_ms: int = 1000) -> bool:
        # Our fixed consumer name resumes unacknowledged work after process death.
        batches = await self.redis.xreadgroup(self.group, self.consumer, {key: "0"}, count=1)
        if not any(messages for _, messages in batches):
            batches = await self.redis.xreadgroup(
                self.group, self.consumer, {key: ">"}, count=1, block=block_ms
            )
        for _, messages in batches:
            for entry_id, fields in messages:
                event = EVENTS[fields["type"]](**json.loads(fields["payload"]))
                queue_age_ms = max(0, time.time() * 1000 - int(entry_id.split("-", 1)[0]))
                with event_timing(event, queue_age_ms=round(queue_age_ms, 3)):
                    await self.service.process(event)
                async with self.redis.pipeline(transaction=True) as transaction:
                    transaction.xack(key, self.group, entry_id)
                    transaction.xdel(key, entry_id)
                    await transaction.execute()
                return True
        return False


def snapshot(conversation=None):
    row = conversation or {"id": 23, "status": "open", "custom_attributes": {}}
    assignee, team = row.get("assignee_id"), row.get("team_id")
    return {
        "schema": 1, "capability": CAPABILITY, "features": [PRIVATE_NOTES_FEATURE],
        "input_id": 43, "contact_id": 7, "inbox_id": 3,
        "version": "2026-10-22T10:00:00.123456Z",
        "conversation": {
            "id": row["id"], "status": row["status"],
            "custom_attributes": deepcopy(row["custom_attributes"]),
            "assignee_id": assignee, "team_id": team,
            "meta": {"assignee": {"id": assignee} if assignee else None,
                     "assignee_type": "User" if assignee else "AgentBot",
                     "team": {"id": team} if team else None},
        },
    }


def payload():
    return {
        "event": "message_created", "id": 43, "message_type": "incoming",
        "private": False, "content": "Button",
        "content_attributes": {"nevidimy_telegram_callback_data": "need:legal"},
        "conversation": {"id": 23, "channel": "Channel::Telegram"},
        "sender": {"id": 7}, "inbox": {"id": 3},
        "nevidimy_button_snapshot": snapshot(),
    }


async def test_only_verified_hmac_retains_snapshot_before_enqueue():
    queue = SimpleNamespace(enqueue=AsyncMock())
    webhook = AgentBotWebhook(RecordingService(), route_secret="test", signature_secret="safe-test",
                              event_queue=queue)
    request = _signed_request(payload(), secret="safe-test")
    assert (await webhook.handle(request)).status == 204
    received = queue.enqueue.call_args.args[0]
    assert received.button_snapshot == snapshot()
    request.body = request.body.replace(b"123456", b"123457")
    assert (await webhook.handle(request)).status == 401
    assert queue.enqueue.await_count == 1


async def test_unsigned_legacy_event_never_trusts_supplied_snapshot():
    queue = SimpleNamespace(enqueue=AsyncMock())
    webhook = AgentBotWebhook(RecordingService(), route_secret="test", event_queue=queue)
    assert (await webhook.handle(FakeRequest(json.dumps(payload()).encode(), {}))).status == 204
    assert queue.enqueue.call_args.args[0].button_snapshot is None
    assert parse_message_created(payload()).button_snapshot is None


@pytest.mark.parametrize("mutation", [
    lambda p: p["nevidimy_button_snapshot"].update(input_id=True),
    lambda p: p["nevidimy_button_snapshot"].update(input_id=44),
    lambda p: p["nevidimy_button_snapshot"].update(contact_id=8),
    lambda p: p["nevidimy_button_snapshot"].update(inbox_id=4),
    lambda p: p["nevidimy_button_snapshot"]["conversation"].update(id=24),
    lambda p: p["nevidimy_button_snapshot"].update(schema=2),
    lambda p: p["nevidimy_button_snapshot"].update(version="invalid"),
    lambda p: p["nevidimy_button_snapshot"].update(eligible=True),
    lambda p: p["nevidimy_button_snapshot"].update(reply_id=99),
    lambda p: p["nevidimy_button_snapshot"]["conversation"].update(assignee_id=True),
    lambda p: p["nevidimy_button_snapshot"]["conversation"].update(meta={}),
    lambda p: p["nevidimy_button_snapshot"]["conversation"].update(custom_attributes=[]),
    lambda p: p["nevidimy_button_snapshot"]["conversation"]["custom_attributes"].update(x="x" * 262_144),
    lambda p: p["conversation"].update(channel="Channel::Api"),
])
def test_invalid_snapshot_keeps_original_actionable_event(mutation):
    value = payload()
    mutation(value)
    received = parse_message_created(value, trusted_snapshot=True)
    assert received is not None and received.message_id == 43
    assert received.button_snapshot is None


def test_valid_snapshot_is_copied_and_identity_bound():
    value = payload()
    received = parse_message_created(value, trusted_snapshot=True)
    assert received.button_snapshot == snapshot()
    value["nevidimy_button_snapshot"]["conversation"]["custom_attributes"]["x"] = "changed"
    assert received.button_snapshot["conversation"]["custom_attributes"] == {}


async def test_valid_snapshot_removes_initial_get_without_live_planning_effects(fast_runtime):
    api, service, gateway = fast_runtime
    incoming = replace(event("need:legal", 43), button_snapshot=snapshot(api.conversation))
    assert await service.process(incoming)
    assert api.snapshot_calls == [] and len(api.commits) == 1
    assert api.commits[0][2] == incoming.button_snapshot["version"]
    assert not api.legacy_calls and gateway.calls == 0


async def test_stale_snapshot_discards_plan_then_uses_one_fresh_get(fast_runtime):
    api, service, gateway = fast_runtime
    api.commit_mode = "stale_once"
    assert await service.process(replace(event("need:legal", 43),
                                         button_snapshot=snapshot(api.conversation)))
    assert api.snapshot_calls == [(23, 43)] and len(api.commits) == 2
    assert api.commits[1][2] == "fresh-version:1"
    assert not api.legacy_calls and gateway.calls == 0


async def test_unknown_snapshot_commit_does_not_get_or_send_again(fast_runtime):
    api, service, _ = fast_runtime
    api.commit_mode = "unknown"
    with pytest.raises(ConnectionError):
        await service.process(replace(event("need:legal", 43),
                                      button_snapshot=snapshot(api.conversation)))
    assert len(api.commits) == 1 and not api.snapshot_calls and not api.legacy_calls


async def test_stale_snapshot_then_human_takeover_never_commits_old_plan(fast_runtime):
    api, service, _ = fast_runtime
    incoming = replace(event("need:legal", 43), button_snapshot=snapshot(api.conversation))
    api.commit_mode = "stale_once"

    def takeover():
        api.conversation["assignee_id"] = 4
        api.eligible = False

    api.after_stale = takeover
    await service.process(incoming)
    assert api.snapshot_calls == [(23, 43)] and len(api.commits) == 1
    assert api.conversation["custom_attributes"]["reply_owner"] == "human"


async def test_repeated_snapshot_conflicts_remain_bounded(fast_runtime):
    api, service, _ = fast_runtime
    api.commit_mode = "stale"
    incoming = replace(event("need:legal", 43), button_snapshot=snapshot(api.conversation))
    assert await service.process(incoming)
    assert api.snapshot_calls == [(23, 43)] and len(api.commits) == 2
    assert api.legacy_calls.count("reply") == 1


async def test_no_plan_from_old_snapshot_requires_fresh_read_not_silent_ack(fast_runtime):
    api, service, _ = fast_runtime
    old = snapshot(api.conversation)
    old["conversation"]["custom_attributes"]["scenario_last_message_id"] = 44
    assert await service.process(replace(event("need:legal", 43), button_snapshot=old))
    assert api.snapshot_calls == [(23, 43)] and len(api.commits) == 1


async def test_snapshot_never_skips_legacy_pending_checkpoint_cleanup(fast_runtime):
    api, service, gateway = fast_runtime
    api.eligible = False
    api.duplicate_reply_id = api.replies[-1]["message_id"]
    api.conversation["custom_attributes"]["scenario_pending_input"] = {"message_id": 42}
    supplied = snapshot(api.conversation)
    supplied["input_id"] = 42
    assert not await service.process(replace(event("continue", 42), button_snapshot=supplied))
    assert api.snapshot_calls == [(23, 42)] and not api.commits
    assert "history" in api.legacy_calls and "reply" not in api.legacy_calls
    assert api.conversation["custom_attributes"]["scenario_pending_input"] is None
    assert gateway.calls == 0


async def test_queue_old_schema_and_snapshot_schema_preserve_retry_identity():
    original = IncomingChatwootMessage(43, 23, 7, 3, "need:legal")
    redis = SimpleNamespace(xadd=AsyncMock())
    service = SimpleNamespace(process=AsyncMock(side_effect=RuntimeError("safe-test")))
    queue = DurableEventQueue(redis, service, namespace="neutral", lanes=1)
    await queue.enqueue(original)
    fields = redis.xadd.call_args.args[1]
    assert "button_snapshot" not in json.loads(fields["payload"])
    assert IncomingChatwootMessage(**json.loads(fields["payload"])) == original
    extended = replace(original, button_snapshot=snapshot())
    await queue.enqueue(extended)
    fields = redis.xadd.call_args.args[1]
    redis.xreadgroup = AsyncMock(return_value=[("neutral:0", [("1000-0", fields)])])
    with pytest.raises(RuntimeError):
        await queue.consume_one("neutral:0")
    restored = service.process.call_args.args[0]
    assert asdict(restored) == asdict(extended)
    transaction = MagicMock()
    transaction.__aenter__.return_value = transaction
    transaction.execute = AsyncMock()
    redis.pipeline = MagicMock(return_value=transaction)
    service.process = AsyncMock(return_value=True)
    restarted = DurableEventQueue(redis, service, namespace="neutral", lanes=1)
    assert await restarted.consume_one("neutral:0")
    assert asdict(service.process.call_args.args[0]) == asdict(extended)
    transaction.xack.assert_called_once_with("neutral:0", "agent", "1000-0")
    transaction.xdel.assert_called_once_with("neutral:0", "1000-0")
    legacy_fields = set(asdict(original)) - {"button_snapshot"}
    assert set(json.loads(fields["payload"])) == legacy_fields
    assert set(fields) == {"type", "payload", "button_snapshot"}
    assert json.loads(fields["button_snapshot"]) == snapshot()
    assert EVENTS[fields["type"]](**json.loads(fields["payload"])).message_id == 43


async def test_snapshot_survives_real_redis_pending_restart_and_ack(redis):
    incoming = IncomingChatwootMessage(43, 23, 7, 3, "need:legal", snapshot())
    service = RecordingService()
    queue = DurableEventQueue(redis, service, namespace="neutral-snapshot", lanes=1)
    key = queue.keys[0]
    await redis.xgroup_create(key, queue.group, id="0", mkstream=True)
    await queue.enqueue(incoming)
    await redis.xreadgroup(queue.group, queue.consumer, {key: ">"}, count=1)
    assert (await redis.xpending(key, queue.group))["pending"] == 1
    restarted = DurableEventQueue(redis, service, namespace="neutral-snapshot", lanes=1)
    assert await restarted.consume_one(key)
    assert service.events == [incoming]
    assert (await redis.xpending(key, queue.group))["pending"] == 0
    assert await redis.xlen(key) == 0


async def test_actual_old_reader_consumes_new_hint_after_pending_restart(redis):
    incoming = IncomingChatwootMessage(43, 23, 7, 3, "need:legal", snapshot())
    service = RecordingService()
    queue = DurableEventQueue(redis, service, namespace="neutral-old-reader", lanes=1)
    key = queue.keys[0]
    await redis.xgroup_create(key, queue.group, id="0", mkstream=True)
    await queue.enqueue(incoming)
    await redis.xreadgroup(queue.group, queue.consumer, {key: ">"}, count=1)
    old_reader = LegacyEventQueue(redis, service, namespace="neutral-old-reader", lanes=1)
    assert await old_reader.consume_one(key)
    expected = asdict(incoming)
    expected.pop("button_snapshot")
    assert asdict(service.events[0]) == expected
    assert not hasattr(service.events[0], "button_snapshot")
    assert (await redis.xpending(key, queue.group))["pending"] == 0 and await redis.xlen(key) == 0


@pytest.mark.parametrize("hint", ["invalid-json", "null", "x" * 262_145,
                                  json.dumps({**snapshot(), "input_id": 44})])
async def test_malformed_stream_hint_does_not_poison_or_drop_event(redis, hint):
    service = RecordingService()
    queue = DurableEventQueue(redis, service, namespace="neutral-invalid", lanes=1)
    key = queue.keys[0]
    await redis.xgroup_create(key, queue.group, id="0", mkstream=True)
    incoming = IncomingChatwootMessage(43, 23, 7, 3, "need:legal")
    fields = asdict(incoming)
    fields.pop("button_snapshot")
    await redis.xadd(key, {"type": "IncomingChatwootMessage", "payload": json.dumps(fields),
                          "button_snapshot": hint})
    assert await queue.consume_one(key)
    assert service.events == [incoming]
    assert (await redis.xpending(key, queue.group))["pending"] == 0 and await redis.xlen(key) == 0

"""Request-budget regressions with neutral data; no external services or models."""

from datetime import UTC, datetime, timedelta

import pytest

from app.chatwoot.client import ChatwootClient
from app.chatwoot.followups import sweep
from app.chatwoot.service import ChatwootAgentService
from tests.test_chatwoot_client import RecordingTransport, client
from tests.test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation


async def test_loaded_history_can_be_reused_for_initial_deduplication():
    transport = RecordingTransport()
    api = client(transport)
    messages = ({"id": 51, "content_attributes": {"bot_turn_key": "message:41"}},)
    assert await api.has_reply_for_turn(23, "message:41", messages=messages)
    assert not await api.has_reply_for_turn(23, "message:42", messages=())
    assert not transport.calls


async def test_fresh_tail_uses_native_id_filter_without_assuming_timestamp_order():
    path = "/api/v1/accounts/12/conversations/23/messages"
    ids = list(range(51, 81))[::-1]
    transport = RecordingTransport(responses={
        ("GET", path + "?after=50"): {"payload": [{"id": n} for n in ids]},
    })
    messages = await client(transport).get_messages(23, after_id=50)
    assert {m["id"] for m in messages} == set(range(51, 81))
    assert len(transport.calls) == 1


async def test_saturated_native_tail_falls_back_to_complete_history():
    path = "/api/v1/accounts/12/conversations/23/messages"
    responses = {("GET", path + "?after=50"): {
        "payload": [{"id": n} for n in range(51, 151)],
    }}
    before = 151
    for _ in range(8):
        suffix = "" if before == 151 else f"?before={before}"
        ids = list(range(max(1, before - 20), before))
        responses["GET", path + suffix] = {"payload": [{"id": n} for n in ids]}
        before = min(ids)
    transport = RecordingTransport(responses=responses)
    messages = await client(transport).get_messages(23, after_id=50)
    assert {m["id"] for m in messages} == set(range(51, 151))
    assert len(transport.calls) == 9


async def test_service_reads_history_once_and_keeps_both_fresh_guards():
    class Api(FakeChatwoot):
        reads = None

        async def get_messages(self, cid, *, after_id=None):
            self.reads.append(after_id)
            messages = self.messages + tuple({
                "id": r["message_id"], "message_type": "outgoing", "private": False,
                "content_attributes": {"bot_turn_key": r["turn_key"]},
            } for r in self.replies)
            return tuple(m for m in messages if after_id is None or m["id"] > after_id)

        has_reply_for_turn = ChatwootClient.has_reply_for_turn

    api = Api()
    api.reads = []
    gateway = StubGateway(ordinary_evaluation())
    bot = ChatwootAgentService(api, gateway=gateway)
    assert await bot.process(event("/start", 41))
    assert api.reads == [None, 41, 41]
    api.reads.clear()
    assert await bot.process(event("continue", 42))
    assert api.reads == [None, 42, 42]
    assert gateway.calls == 0
    assert not await bot.process(event("continue", 42))
    assert len(api.replies) == 2


async def test_reply_appearing_after_initial_snapshot_still_prevents_duplicate():
    class Api(FakeChatwoot):
        has_reply_for_turn = ChatwootClient.has_reply_for_turn

        async def set_custom_attributes(self, cid, attributes):
            await super().set_custom_attributes(cid, attributes)
            if "workflow_state" in attributes:
                self.messages += ({"id": 51, "content_attributes": {
                    "bot_turn_key": "message:41",
                }},)

    api = Api()
    bot = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    assert not await bot.process(event("/start", 41))
    assert not api.replies


async def test_scheduler_skips_future_and_terminal_jobs_but_rereads_due_candidates():
    now = datetime(2026, 10, 22, 9, tzinfo=UTC)
    due = (now - timedelta(minutes=1)).isoformat()
    future = (now + timedelta(days=1)).isoformat()

    class Api:
        async def list_conversations(self, page):
            if page != 1:
                return ()
            return tuple({"id": n, "custom_attributes": {"scenario_followups": {
                "job": {"state": state, "due_at": when},
            }}} for n, state, when in (
                (1, "pending", due), (2, "pending", future), (3, "sent", due),
                (4, "cancelled", due), (5, "answered", due), (6, "expired", due),
            ))

    class Service:
        def __init__(self):
            self.seen = []

        async def send_due_followup(self, cid):
            self.seen.append(cid)
            return False  # The authoritative reread can cancel a stale list candidate.

    service = Service()
    assert await sweep(Api(), service, now=now) == 0
    assert service.seen == [1]


async def test_scheduler_does_not_scan_when_delivery_is_outside_working_hours():
    class Api:
        async def list_conversations(self, page):
            pytest.fail("No followup can be due outside the existing delivery window")

    assert await sweep(Api(), None, now=datetime(2026, 10, 22, 0, tzinfo=UTC)) == 0


async def test_scheduler_discovers_rescheduling_on_next_sweep_and_isolates_invalid_jobs():
    now = datetime(2026, 10, 22, 9, tzinfo=UTC)

    class Api:
        due_at = (now + timedelta(days=1)).isoformat()

        async def list_conversations(self, page):
            if page != 1:
                return ()
            return tuple({"id": n, "custom_attributes": {"scenario_followups": {
                "job": {"state": "pending", "due_at": when},
            }}} for n, when in ((1, "invalid"), (2, self.due_at)))

    class Service:
        def __init__(self):
            self.seen = []

        async def send_due_followup(self, cid):
            self.seen.append(cid)
            return True

    api, service = Api(), Service()
    assert await sweep(api, service, now=now) == 0
    api.due_at = now.isoformat()
    assert await sweep(api, service, now=now) == 1
    assert service.seen == [2]

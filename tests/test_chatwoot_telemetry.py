"""Content-free correlation, concurrency and cancellation; neutral fixtures only."""

import asyncio
import json
import logging

import pytest

from app.chatwoot import telemetry
from app.chatwoot.contracts import ConversationChanged, IncomingChatwootMessage
from app.chatwoot.service import ChatwootAgentService
from tests.test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation


@pytest.fixture(autouse=True)
def capture_timings(caplog):
    caplog.set_level(logging.INFO, logger=telemetry.__name__)


def records(caplog, kind):
    return [json.loads(r.getMessage().partition(" ")[2]) for r in caplog.records
            if r.name == telemetry.__name__ and r.getMessage().startswith(kind + " ")]


def test_timings_exclude_content_queries_credentials_and_exception_text(caplog):
    incoming = IncomingChatwootMessage(41, 23, 91, 12, "synthetic-private-content")
    with pytest.raises(ValueError), telemetry.event_timing(incoming, queue_age_ms=4) as timing:
        with (
            telemetry.timed_stage("history"),
            telemetry.api_timing("GET", "/api/v1/accounts/12/conversations/23/messages?secret=private") as api,
        ):
            api.status = 200
            api.request_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with telemetry.api_timing("private-method", "https://user:private@internal/private") as api:
            api.request_id = "synthetic-private-header"
        telemetry.reply_persisted(51)
        timing.replied = True
        raise ValueError("synthetic-private-exception")
    serialized = caplog.text
    assert "private" not in serialized and "https://" not in serialized
    calls = records(caplog, "chatwoot_api_timing")
    assert calls[0]["operation"] == "conversations.messages"
    assert calls[0]["stage"] == "history" and calls[0]["status"] == 200
    assert calls[0]["request_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert calls[1]["operation"] == calls[1]["method"] == "other"
    assert calls[1]["request_id"] is None
    summary, = records(caplog, "chatwoot_event_timing")
    assert summary["conversation"] == 23 and summary["message"] == 41
    assert summary["queue_age_ms"] == 4 and summary["error_type"] == "ValueError"
    assert summary["api_count"] == 2 and summary["reply_ids"] == [51]
    assert summary["replied"] and summary["stages_ms"]["history"] >= 0
    assert summary["total_ms"] >= summary["api_ms"] >= 0


async def test_concurrent_tasks_keep_correlations_isolated_and_clear_context(caplog):
    async def measure(number):
        incoming = IncomingChatwootMessage(number, number * 10, 91, 12, "neutral")
        with telemetry.event_timing(incoming):
            await asyncio.sleep(0)
            with telemetry.api_timing("GET", "/unrecognized"):
                await asyncio.sleep(0)
            telemetry.reply_persisted(number + 100)

    await asyncio.gather(measure(1), measure(2))
    summaries = sorted(records(caplog, "chatwoot_event_timing"), key=lambda r: r["message"])
    assert [(r["message"], r["conversation"], r["reply_ids"], r["api_count"])
            for r in summaries] == [(1, 10, [101], 1), (2, 20, [102], 1)]
    before = len(caplog.records)
    with telemetry.event_timing(ConversationChanged(23)), telemetry.api_timing("GET", "/other"):
        telemetry.reply_persisted(999)
    assert len(caplog.records) == before


async def test_nested_service_trace_has_one_summary_and_no_extra_requests(caplog):
    api = FakeChatwoot()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    incoming = event("/start", 41)
    with telemetry.event_timing(incoming, queue_age_ms=3):
        assert await service.process(incoming)
    summary, = records(caplog, "chatwoot_event_timing")
    assert summary["replied"] and summary["queue_age_ms"] == 3
    assert {"history", "scenario", "lock_wait", "staff_guard", "reply_write"} <= summary["stages_ms"].keys()
    caplog.clear()
    assert not await service.process(incoming)
    summary, = records(caplog, "chatwoot_event_timing")
    assert not summary["replied"] and len(api.replies) == 1


async def test_cancelled_lock_wait_is_logged_without_releasing_another_task_lock(caplog):
    service = ChatwootAgentService(FakeChatwoot(), gateway=StubGateway(ordinary_evaluation()))
    incoming = event("/start", 41)
    lock = service._locks.setdefault(incoming.conversation_id, asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(service.process(incoming))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.locked()
    lock.release()
    summary, = records(caplog, "chatwoot_event_timing")
    assert summary["error_type"] == "CancelledError" and not summary["replied"]
    caplog.clear()
    assert await service.process(incoming)
    assert records(caplog, "chatwoot_event_timing")[0]["error_type"] is None

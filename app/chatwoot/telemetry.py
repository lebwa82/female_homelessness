"""Content-free per-event timings; no URLs, payloads, tokens or exception text."""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.chatwoot.contracts import IncomingChatwootMessage
from app.chatwoot.metrics import kind_label, method_label, metrics, status_label

logger = logging.getLogger(__name__)
_current: ContextVar[EventTiming | None] = ContextVar("chatwoot_timing", default=None)


def configure() -> None:
    # Do not enable verbose HTTP/provider logs along with our bounded metrics.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logger.setLevel(logging.INFO)


@dataclass
class EventTiming:
    conversation_id: int
    message_id: int
    queue_age_ms: float | None
    started: float = field(default_factory=time.perf_counter)
    started_at_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)
    kind: str = "incoming"
    stage: str = "processing"
    stages_ms: dict[str, float] = field(default_factory=dict)
    api_count: int = 0
    api_ms: float = 0
    replied: bool = False
    reply_ids: list[int] = field(default_factory=list)
    error_type: str | None = None

    def identity(self) -> dict:
        return {"conversation": self.conversation_id, "message": self.message_id}


@contextmanager
def event_timing(event, *, queue_age_ms=None):
    existing = _current.get()
    if existing is not None or not isinstance(event, IncomingChatwootMessage):
        yield existing
        return
    timing = EventTiming(event.conversation_id, event.message_id, queue_age_ms)
    token = _current.set(timing)
    try:
        yield timing
    except BaseException as error:
        timing.error_type = type(error).__name__
        raise
    finally:
        _current.reset(token)
        elapsed = time.perf_counter() - timing.started
        metrics.event_completed(timing, elapsed)
        logger.info(
            "chatwoot_event_timing %s",
            json.dumps(
                {
                    **timing.identity(),
                    "started_at_ms": timing.started_at_ms,
                    "kind": timing.kind,
                    "queue_age_ms": timing.queue_age_ms,
                    "total_ms": round(elapsed * 1000, 3),
                    "api_count": timing.api_count,
                    "api_ms": round(timing.api_ms, 3),
                    "stages_ms": {key: round(value, 3) for key, value in timing.stages_ms.items()},
                    "replied": timing.replied,
                    "reply_ids": timing.reply_ids,
                    "error_type": timing.error_type,
                }
            ),
        )


@contextmanager
def timed_stage(name):
    timing = _current.get()
    started = time.perf_counter()
    previous = timing.stage if timing else None
    if timing:
        timing.stage = name
    try:
        yield
    finally:
        if timing:
            timing.stages_ms[name] = (
                timing.stages_ms.get(name, 0) + (time.perf_counter() - started) * 1000
            )
            timing.stage = previous


@dataclass
class ApiTiming:
    status: int = 0
    request_id: str = ""


@contextmanager
def api_timing(method, path):
    timing = _current.get()
    started = time.perf_counter()
    response = ApiTiming()
    try:
        yield response
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        # Same bounded route labels as the logs; never include IDs or queries.
        match = re.fullmatch(
            r"/api/v1/accounts/\d+/(conversations|teams|inboxes)(?:/\d+)?"
            r"(?:/(messages|custom_attributes|assignments|toggle_status|team_members))?",
            path.partition("?")[0],
        )
        operation = ".".join(filter(None, match.groups())) if match else "other"
        method = method_label(method)
        metrics.api.labels(operation, method, status_label(response.status)).observe(elapsed / 1000)
        if timing:
            timing.api_count += 1
            timing.api_ms += elapsed
            request_id = (
                response.request_id
                if re.fullmatch(
                    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                    response.request_id,
                )
                else None
            )
            logger.info(
                "chatwoot_api_timing %s",
                json.dumps(
                    {
                        **timing.identity(),
                        "stage": timing.stage,
                        "method": method,
                        "operation": operation,
                        "status": response.status,
                        "request_id": request_id,
                        "elapsed_ms": round(elapsed, 3),
                    }
                ),
            )


def reply_persisted(message_id):
    timing = _current.get()
    if timing and type(message_id) is int:
        timing.reply_ids.append(message_id)
        elapsed = (time.perf_counter() - timing.started) * 1000
        if len(timing.reply_ids) == 1:
            metrics.reply.labels(kind_label(timing.kind)).observe(elapsed / 1000)
        logger.info(
            "chatwoot_reply_timing %s",
            json.dumps(
                {
                    **timing.identity(),
                    "outgoing_message": message_id,
                    "elapsed_ms": round(elapsed, 3),
                }
            ),
        )

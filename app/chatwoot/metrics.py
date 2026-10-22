"""Bounded, content-free Prometheus metrics for the single-process backend."""

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Histogram, generate_latest

DURATION_BUCKETS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    0.75,
    1,
    1.5,
    2,
    3,
    5,
    8,
    13,
    20,
    45,
)
STAGES = frozenset(
    {
        "processing",
        "lock_wait",
        "history",
        "scenario",
        "ownership_guard",
        "staff_guard",
        "scenario_effects",
        "workflow_write",
        "reply_write",
    }
)


def method_label(method: str) -> str:
    return (
        method
        if method in {"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"}
        else "other"
    )


def status_label(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status < 600 else "error"


def kind_label(kind: str) -> str:
    return kind if kind in {"callback", "text"} else "other"


class RequestMetrics:
    def __init__(self) -> None:
        # Separate registry: no machine names, process paths or default collectors.
        self.registry = CollectorRegistry()
        self.api = Histogram(
            "nevidimy_chatwoot_api_request_duration_seconds",
            "Chatwoot API duration, including calls outside incoming-message processing.",
            ("operation", "method", "status_class"),
            registry=self.registry,
            buckets=DURATION_BUCKETS,
        )
        self.http = Histogram(
            "nevidimy_http_request_duration_seconds",
            "Backend HTTP request duration, not bot reply latency.",
            ("endpoint", "method", "status_class"),
            registry=self.registry,
            buckets=DURATION_BUCKETS,
        )
        self.event = Histogram(
            "nevidimy_event_processing_duration_seconds",
            "Incoming-message processing attempt duration.",
            ("kind", "outcome"),
            registry=self.registry,
            buckets=DURATION_BUCKETS,
        )
        self.queue = Histogram(
            "nevidimy_event_queue_age_seconds",
            "Redis event age at processing start; includes retries.",
            ("kind",),
            registry=self.registry,
            buckets=DURATION_BUCKETS,
        )
        self.stage = Histogram(
            "nevidimy_event_stage_duration_seconds",
            "Processing stage duration per incoming attempt.",
            ("kind", "stage"),
            registry=self.registry,
            buckets=DURATION_BUCKETS,
        )
        self.reply = Histogram(
            "nevidimy_first_reply_created_seconds",
            "Time from processing start to first persisted public reply; excludes Telegram delivery.",
            ("kind",),
            registry=self.registry,
            buckets=DURATION_BUCKETS,
        )
        self.api_count = Histogram(
            "nevidimy_event_api_requests",
            "Number of Chatwoot API calls per incoming attempt.",
            ("kind",),
            registry=self.registry,
            buckets=(0, 1, 2, 4, 8, 12, 16, 24, 32, 64),
        )

    def event_completed(self, timing, elapsed: float) -> None:
        kind = kind_label(timing.kind)
        outcome = "error" if timing.error_type else "replied" if timing.replied else "silent"
        self.event.labels(kind, outcome).observe(elapsed)
        self.api_count.labels(kind).observe(timing.api_count)
        if timing.queue_age_ms is not None:
            self.queue.labels(kind).observe(timing.queue_age_ms / 1000)
        for name, value in timing.stages_ms.items():
            self.stage.labels(kind, name if name in STAGES else "other").observe(value / 1000)

    async def expose(self, _request: web.Request) -> web.Response:
        return web.Response(
            body=generate_latest(self.registry),
            headers={
                "Content-Type": CONTENT_TYPE_LATEST,
                "Cache-Control": "no-store",
            },
        )


metrics = RequestMetrics()

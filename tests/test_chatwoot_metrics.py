"""Prometheus metrics contain bounded technical labels, never dialogue or IDs."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import generate_latest

from app.chatwoot import app, telemetry
from app.chatwoot.contracts import IncomingChatwootMessage
from app.chatwoot.metrics import RequestMetrics
from tests.test_chatwoot_app import RecordingService


@pytest.fixture
def metrics(monkeypatch):
    value = RequestMetrics()
    monkeypatch.setattr(telemetry, "metrics", value)
    monkeypatch.setattr(app, "metrics", value)
    return value


def sample(metrics, name, **labels):
    return metrics.registry.get_sample_value(name, labels)


def test_api_metrics_cover_background_calls_without_recording_paths_or_credentials(metrics):
    for number in range(100):
        with telemetry.api_timing(
            "GET", f"/api/v1/accounts/12/conversations/{number}/messages?private=hidden"
        ) as response:
            response.status = 200
            response.request_id = "private-header"
    assert (
        sample(
            metrics,
            "nevidimy_chatwoot_api_request_duration_seconds_count",
            operation="conversations.messages",
            method="GET",
            status_class="2xx",
        )
        == 100
    )
    text = generate_latest(metrics.registry).decode()
    assert "private" not in text and "hidden" not in text
    assert "conversation=" not in text and "request_id=" not in text
    assert "message=" not in text and "/api/" not in text
    # Changing IDs and query parameters never creates additional label sets.
    assert (
        sum(
            s.name.endswith("_count")
            for family in metrics.registry.collect()
            for s in family.samples
        )
        == 1
    )


def test_api_status_classes_and_network_failure_are_observed(metrics):
    for status in (200, 401, 503):
        with telemetry.api_timing("POST", "/unrecognized-private-url") as response:
            response.status = status
    with pytest.raises(TimeoutError), telemetry.api_timing("private-method", "/private"):
        raise TimeoutError("private-error")
    for status in ("2xx", "4xx", "5xx"):
        assert (
            sample(
                metrics,
                "nevidimy_chatwoot_api_request_duration_seconds_count",
                operation="other",
                method="POST",
                status_class=status,
            )
            == 1
        )
    assert (
        sample(
            metrics,
            "nevidimy_chatwoot_api_request_duration_seconds_count",
            operation="other",
            method="other",
            status_class="error",
        )
        == 1
    )
    assert "private" not in generate_latest(metrics.registry).decode()


def test_nested_event_has_one_attempt_and_first_reply_is_not_double_counted(metrics):
    incoming = IncomingChatwootMessage(41, 23, 91, 12, "private-input")
    with telemetry.event_timing(incoming, queue_age_ms=25) as timing:
        timing.kind = "callback"
        with (
            telemetry.event_timing(incoming),
            telemetry.timed_stage("scenario"),
            telemetry.api_timing("GET", "/api/v1/accounts/12/conversations/23") as response,
        ):
            response.status = 200
        telemetry.reply_persisted(51)
        telemetry.reply_persisted(52)
        timing.replied = True
    assert (
        sample(
            metrics,
            "nevidimy_event_processing_duration_seconds_count",
            kind="callback",
            outcome="replied",
        )
        == 1
    )
    assert sample(metrics, "nevidimy_first_reply_created_seconds_count", kind="callback") == 1
    assert sample(metrics, "nevidimy_event_queue_age_seconds_sum", kind="callback") == 0.025
    assert (
        sample(
            metrics,
            "nevidimy_event_stage_duration_seconds_count",
            kind="callback",
            stage="scenario",
        )
        == 1
    )
    assert sample(metrics, "nevidimy_event_api_requests_sum", kind="callback") == 1
    text = generate_latest(metrics.registry).decode()
    assert "private" not in text and "message_id=" not in text


@pytest.mark.parametrize("error", [ValueError("private-exception"), asyncio.CancelledError()])
def test_failed_and_cancelled_attempts_are_measured_without_exception_labels(metrics, error):
    incoming = IncomingChatwootMessage(41, 23, 91, 12, "private-input")
    with pytest.raises(type(error)), telemetry.event_timing(incoming) as timing:
        timing.kind = "private-kind"
        timing.stages_ms["private-stage"] = 5
        raise error
    assert (
        sample(
            metrics,
            "nevidimy_event_processing_duration_seconds_count",
            kind="other",
            outcome="error",
        )
        == 1
    )
    assert (
        sample(metrics, "nevidimy_event_stage_duration_seconds_count", kind="other", stage="other")
        == 1
    )
    assert "private" not in generate_latest(metrics.registry).decode()


async def test_metrics_endpoint_and_http_middleware_preserve_auth_and_hide_route_secret(metrics):
    application = app.create_application(
        RecordingService(), route_secret="private-route", signature_secret="private-signature"
    )
    async with TestClient(TestServer(application)) as client:
        assert (await client.get("/healthz")).status == 200
        assert (await client.post("/webhooks/chatwoot/agent/private-route", json={})).status == 401
        assert (await client.get("/private-path?token=hidden")).status == 404
        first = await client.get("/metrics")
        assert first.status == 200 and first.headers["Cache-Control"] == "no-store"
        assert "text/plain" in first.headers["Content-Type"]
        text = await first.text()
        assert "private" not in text and "hidden" not in text
        assert "webhook" in text
        second = await client.get("/metrics")
        assert await second.text() == text  # Scrapes do not count themselves.
    for endpoint, status in (("health", "2xx"), ("webhook", "4xx"), ("other", "4xx")):
        assert (
            sample(
                metrics,
                "nevidimy_http_request_duration_seconds_count",
                endpoint=endpoint,
                method="POST" if endpoint == "webhook" else "GET",
                status_class=status,
            )
            == 1
        )


async def test_degraded_queue_still_returns_503_and_is_observed(metrics):
    class Queue:
        async def healthy(self):
            return False

    application = app.create_application(
        RecordingService(), route_secret="private", event_queue=Queue()
    )
    async with TestClient(TestServer(application)) as client:
        assert (await client.get("/healthz")).status == 503
    assert (
        sample(
            metrics,
            "nevidimy_http_request_duration_seconds_count",
            endpoint="health",
            method="GET",
            status_class="5xx",
        )
        == 1
    )

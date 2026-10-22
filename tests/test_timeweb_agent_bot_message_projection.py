"""Neutral producer/actual Python parser contract; no customer dialogue fixtures."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.chatwoot.contracts import (
    parse_conversation_changed,
    parse_message_created,
    parse_message_delivery_changed,
    parse_staff_message,
)

ROOT = Path(__file__).resolve().parents[1]


def test_callback_projection_keeps_actual_consumer_event_and_native_fanout():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the producer contract")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_agent_bot_message_projection_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and all(report["checks"].values())
    assert report["performed_jobs"] == 0
    for case in report["cases"]:
        baseline, projected = case["baseline"], case["projected"]
        expected = parse_message_created(baseline)
        assert expected is not None
        assert parse_message_created(projected) == expected
        assert projected["content_attributes"] == baseline["content_attributes"]
        for parser in (parse_staff_message, parse_message_delivery_changed, parse_conversation_changed):
            assert parser(projected) == parser(baseline) is None

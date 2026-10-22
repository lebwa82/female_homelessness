"""Neutral cross-language producer/consumer contract; no customer fixtures."""

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


def test_native_projection_preserves_actual_consumer_contract():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the native event contract")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_agent_bot_conversation_projection_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and report["native_fanout_preserved"] and report["source_guards"]
    for case in report["cases"]:
        baseline, projected = case["baseline"], case["projected"]
        assert parse_conversation_changed(projected) == parse_conversation_changed(baseline)
        assert projected["changed_attributes"] == baseline["changed_attributes"]
        for parser in (parse_message_created, parse_staff_message, parse_message_delivery_changed):
            assert parser(projected) is None and parser(baseline) is None

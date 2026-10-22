"""Neutral HTTPS lifecycle contracts; no sockets, Rails boot or credentials."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_internal_agent_webhook_keepalive_preserves_transport_contract():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the internal webhook keepalive contract")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_agent_webhook_keepalive_probe.rb")],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 16 and all(value is True for value in report.values())

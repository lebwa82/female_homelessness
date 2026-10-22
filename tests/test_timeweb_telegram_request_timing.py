"""Deterministic, content-free native HTTP callback timing contracts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_telegram_request_timing_preserves_native_callback_semantics():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the native request timing contract")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_telegram_request_timing_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 12 and all(report.values())

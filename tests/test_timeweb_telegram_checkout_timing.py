"""Neutral pool attribution contracts: no app imports or network operations."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_telegram_checkout_timing_preserves_delivery_contract():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the checkout timing contract")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_telegram_checkout_timing_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 13 and all(report.values())

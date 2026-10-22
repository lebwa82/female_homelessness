import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_atomic_turn_timing_preserves_native_contract_and_logs_only_fixed_metrics():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the content-free atomic turn timing probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_turn_timing_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 31 and all(report.values())

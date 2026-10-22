import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_interactive_queue_isolation_and_noop_delivery_contracts():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the native job probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_interactive_jobs_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 8 and all(report.values())

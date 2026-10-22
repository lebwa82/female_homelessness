import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_atomic_turn_initializer_with_synthetic_controller_and_orm_contracts():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the Chatwoot atomic turn probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_atomic_turn_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 11 and all(report.values())

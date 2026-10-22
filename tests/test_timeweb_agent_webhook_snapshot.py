import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_late_internal_snapshot_identity_and_final_body_signature():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby required for the neutral webhook probe")
    result = subprocess.run(
        [ruby, str(Path(__file__).with_name("timeweb_agent_webhook_snapshot_probe.rb"))],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 5 and all(report.values())

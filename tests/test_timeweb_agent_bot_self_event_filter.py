import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_marked_agent_bot_self_events_skip_only_proven_unused_native_notifications():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the native self-event filter probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_agent_bot_self_event_filter_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 9 and all(report.values())

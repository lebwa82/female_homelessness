import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_pinned_webhook_presenter_skips_only_discarded_message_serialization():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the native webhook presenter probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_webhook_presenter_fast_path_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 9 and all(report.values())

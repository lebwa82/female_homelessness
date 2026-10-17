import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_optional_avatar_does_not_block_mvp_and_does_not_change_main_bot():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the Chatwoot adapter probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_telegram_avatar_probe.rb")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report and all(report.values())

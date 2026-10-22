import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_telegram_text_transport_reuses_verified_tls_sessions_without_retries():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the Chatwoot transport probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_telegram_transport_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 20 and all(report.values())

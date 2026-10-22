"""Real OpenSSL/Net::HTTP against ephemeral synthetic loopback TLS only."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_internal_agent_webhook_reuses_only_ca_and_hostname_verified_tls():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the neutral webhook TLS probe")
    capability = subprocess.run(
        [ruby, "-rnet/http", "-e",
         "exit([:ipaddr=, :verify_hostname=].all? { |name| Net::HTTP.method_defined?(name) } ? 0 : 77)"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if capability.returncode == 77:
        pytest.skip("Net::HTTP ipaddr/explicit hostname APIs require a newer Ruby; run in the pinned native image")
    assert capability.returncode == 0, capability.stderr
    result = subprocess.run(
        [ruby, str(Path(__file__).with_name("timeweb_agent_webhook_tls_probe.rb"))],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 6 and all(value == 1 for value in report.values())

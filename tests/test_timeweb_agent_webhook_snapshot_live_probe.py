"""Static safety contracts only; Rails/PG acceptance is run by the guarded clone launcher."""

import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).with_name("timeweb_agent_webhook_snapshot_live_probe.rb")


def test_native_snapshot_probe_syntax_and_safety_boundaries():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby required for the clone probe syntax check")
    result = subprocess.run([ruby, "-c", str(SOURCE)], capture_output=True,
                            text=True, check=False, timeout=20)
    assert result.returncode == 0
    source = SOURCE.read_text()
    for proof in (
        "nevidimy_cw419_", "current_database()", "verify-full", "pg_stat_ssl",
        "acceptance_run_id", "Channel::Api", "Integrations::Hook.enabled", "Webhook.where",
        "insert_all!", "ActiveRecord::Rollback", "fixture_retained",
        "while_preventing_writes", "two_exact_selects", "coherent_native_row_schema",
        "default_off", "identity_tamper_not_fallback", "oversize_not_fallback",
        "ActiveJob::QueueAdapters::TestAdapter", "ActiveJob::Base.descendants",
        "ActiveJob::Base.prepend(NoJobExecution)", "Net::HTTP.prepend(NoHttp)",
        "candidate_source", "SNAPSHOT_PROBE=", "relative_wall_ms",
    ):
        assert proof in source
    assert "@trigger.send(:nevidimy_snapshot_body, original)" in source
    assert "@trigger.execute" not in source
    assert "Channel::Telegram.create" not in source
    assert "state.to_json" not in source

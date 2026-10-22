"""Neutral local guard contracts; the native workload runs only in a validated clone."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_yjit_probe_fails_closed_before_clone_writes():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the native YJIT probe contracts")
    program = r"""
require ARGV.fetch(0)
require 'singleton'
class ActionCableListener
  include Singleton
end
class AgentBotListener
  include Singleton
end
module Captain
  class ConversationOutcomeEventListener
    include Singleton
  end
end
probe = NevidimyNativeYjitProbe.new
valid = {
  'expected_database' => 'nevidimy_cw419_012345abcdef', 'account_id' => 1,
  'inbox_id' => 2, 'seed_conversation_id' => 3, 'seed_run_id' => 'neutral_seed',
  'run_id' => 'a' * 40, 'bot_id' => 4, 'expected_yjit' => false
}
checks = {}
probe.configure(JSON.generate(valid))
checks[:valid_config] = probe.config == valid
invalid = [valid.merge('expected_database' => 'chatwoot'),
           valid.merge('expected_database' => 'nevidimy_cw419_not_a_clone'),
           valid.merge('run_id' => 'short'), valid.merge('run_id' => 'neutral_seed'),
           valid.merge('seed_run_id' => 'a' * 40), valid.merge('bot_id' => '4'),
           valid.merge('expected_yjit' => 1), valid.merge('extra' => 1)]
checks[:invalid_configs] = invalid.all? do |value|
  begin
    probe.configure(JSON.generate(value))
    false
  rescue NevidimyNativeYjitProbe::Failure
    true
  end
end
probe.configure(JSON.generate(valid))
connection = Object.new
encrypted = true
tls_option = { keyword: 'sslmode', val: 'verify-full' }
# Other conninfo values must never be inspected, even by this diagnostic.
unrelated = { keyword: 'unrelated' }
unrelated.default_proc = proc { raise 'non-whitelisted connection value read' }
raw = Object.new
raw.define_singleton_method(:conninfo) { [unrelated, tls_option] }
connection.define_singleton_method(:raw_connection) { raw }
connection.define_singleton_method(:adapter_name) { 'PostgreSQL' }
connection.define_singleton_method(:open_transactions) { 0 }
connection.define_singleton_method(:select_value) do |sql|
  sql.include?('current_database') ? 'nevidimy_cw419_012345abcdef' : encrypted
end
probe.validate_database(connection, database: valid['expected_database'])
checks[:valid_database] = true
checks[:wrong_database] = begin
  probe.validate_database(connection, database: 'chatwoot')
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
checks[:insecure_database] = begin
  tls_option[:val] = 'require'
  probe.validate_database(connection, database: valid['expected_database'])
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
checks[:missing_effective_tls_mode] = begin
  tls_option.delete(:val)
  probe.validate_database(connection, database: valid['expected_database'])
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
checks[:unencrypted_connection] = begin
  tls_option[:val] = 'verify-full'
  encrypted = false
  probe.validate_database(connection, database: valid['expected_database'])
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
stats = probe.numeric_yjit_stats(inline_code_size: 12, ratio_in_yjit: 0.5,
                                 unknown: 999, vm_insns_count: Float::INFINITY,
                                 yjit_insns_count: 'not numeric')
checks[:stats_whitelist] = stats == { inline_code_size: 12, ratio_in_yjit: 0.5 }
checks[:rss_metadata_only] = probe.memory_from_status("Name:\tneutral\nVmRSS:\t123 kB\nVmHWM:\t456 kB\n") ==
  { rss_kib: 123, peak_rss_kib: 456 }
adapter = Struct.new(:perform_enqueued_jobs, :perform_enqueued_at_jobs, :enqueued_jobs, :performed_jobs).
  new(false, false, [], [])
probe.instance_variable_set(:@adapter, adapter)
checks[:empty_job_capture] = probe.check_jobs == {}
adapter.performed_jobs << Object.new
checks[:job_execution_rejected] = begin
  probe.check_jobs
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
checks[:execution_guard] = begin
  Object.new.extend(NevidimyNativeYjitProbe::NoJobExecution).perform_now
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
dispatcher = Object.new
listeners = [ActionCableListener.instance, AgentBotListener.instance, Captain::ConversationOutcomeEventListener.instance]
dispatcher.define_singleton_method(:listeners) { listeners }
probe.validate_sync_listeners(dispatcher)
checks[:pinned_enterprise_sync_listeners] = true
checks[:unknown_sync_listener_rejected] = begin
  listeners = [*listeners, Object.new]
  probe.validate_sync_listeners(dispatcher)
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
checks[:reordered_sync_listeners_rejected] = begin
  listeners = [AgentBotListener.instance, ActionCableListener.instance, Captain::ConversationOutcomeEventListener.instance]
  probe.validate_sync_listeners(dispatcher)
  false
rescue NevidimyNativeYjitProbe::Failure
  true
end
raise 'neutral contract failed' unless checks.values.all?
puts JSON.generate(checks)
"""
    result = subprocess.run(
        [ruby, "-e", program, str(ROOT / "tests/timeweb_native_yjit_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 15 and all(report.values())

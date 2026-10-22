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


def test_cpu_profile_is_explicit_bounded_and_preserves_sample_workload():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the CPU profile contracts")
    program = r"""
require ARGV.fetch(0)
probe = NevidimyNativeYjitProbe.new
checks = {}
loads = 0
profiler = Object.new
profile_active = false
indices = []
yield_states, setup_states, teardown_states = [], [], []
starts = stops = results_calls = 0
failure = nil
reject_start = false
module ActiveSupport
  module Notifications
    def self.monotonic_subscribe(*)
      Object.new
    end

    def self.unsubscribe(*)
    end
  end
end
report = { mode: :cpu, interval: 1000, samples: 1500, missed_samples: 2, gc_samples: 3,
           frames: {} }
40.times do |index|
  report[:frames][index] = { name: 'Message#dispatch_create_events',
                           file: '/app/app/models/message.rb', line: 389,
                           samples: index, total_samples: index + 1 }
end
report[:frames][100] = { name: 'unknown', file: '/tmp/private-input.rb', line: 1, samples: 99, total_samples: 99 }
report[:frames][101] = { name: 'private text with spaces', file: '/app/app/models/message.rb', line: 1,
                        samples: 99, total_samples: 99 }
report[:frames][102] = { name: 'Message#safe', file: '/app/app/../../private.rb', line: 1,
                        samples: 99, total_samples: 99 }
report[:frames][103] = { name: 'Message#safe', file: '/app/app/models/message.rb', line: 0,
                        samples: 99, total_samples: 99 }
report[:frames][104] = { name: 'Hash#each_pair', file: '<cfunc>', line: nil, samples: 120, total_samples: 130 }
report[:frames][105] = { name: 'JSON::Ext::Generator::State#generate', file: '<cfunc>', line: nil,
                        samples: 80, total_samples: 90 }
report[:frames][106] = { name: 'OpenSSL::HMAC.digest', file: '<cfunc>', line: nil, samples: 70, total_samples: 75 }
report[:frames][107] = { name: 'Hash#each_pair', file: nil, line: nil, samples: 10, total_samples: 20 }
report[:frames][108] = { name: 'Hash#each_pair', file: '<cfunc>', line: 0, samples: 10, total_samples: 20 }
report[:frames][109] = { name: '#<Class:0x123>#private', file: '<cfunc>', line: nil, samples: 10, total_samples: 20 }
report[:frames][110] = { name: 'Hash#each_pair', file: '<cfunc>', samples: 10, total_samples: 20 }
report[:frames][111] = { name: '(marking)', file: '', samples: 3, total_samples: 3 }
report[:frames][112] = { name: '(garbage collection)', file: '', samples: 0, total_samples: 3 }
report[:frames][113] = { name: 'puts private', file: '<cfunc>', line: nil, samples: 10, total_samples: 20 }
profiler.define_singleton_method(:run) { |*| raise 'whole-pair profiling is forbidden' }
profiler.define_singleton_method(:start) do |**options|
  raise 'wrong CPU options' unless options == { mode: :cpu, interval: 1000, raw: false }
  raise 'nested CPU profile' if profile_active
  starts += 1
  next false if reject_start

  profile_active = true
  true
end
profiler.define_singleton_method(:stop) do
  raise 'CPU profile stopped without start' unless profile_active
  stops += 1
  profile_active = false
  true
end
profiler.define_singleton_method(:results) do
  raise 'CPU results retrieved while running' if profile_active
  results_calls += 1
  report
end
probe.define_singleton_method(:load_cpu_profiler) { loads += 1; profiler }
probe.define_singleton_method(:pair) do |index|
  indices << [index, profile_active]
  setup_states << profile_active
  %w[incoming outgoing].each do |kind|
    metrics do
      yield_states << [index, kind, profile_active]
      raise failure if failure

      :neutral_message
    end
  end
  teardown_states << profile_active
  { neutral_pair: index }
end
probe.configure_cpu_profile(nil)
ordinary = probe.sample_pairs
checks[:ordinary_no_dependency] = loads.zero?
checks[:ordinary_samples_unchanged] = ordinary == (100...130).map { |index| { neutral_pair: index } } &&
  indices == (100...130).map { |index| [index, false] } && yield_states.length == 60 &&
  yield_states.none?(&:last) && probe.instance_variable_get(:@cpu_profile).nil?
probe.configure_cpu_profile('')
checks[:empty_optin_is_off] = loads.zero?
checks[:unknown_mode_fails_closed] = begin
  probe.configure_cpu_profile('wall')
  false
rescue NevidimyNativeYjitProbe::Failure => error
  error.message == 'cpu_profile_mode' && loads.zero?
end
indices.clear
probe.configure_cpu_profile('cpu')
probe.metrics { checks[:loaded_profiler_does_not_profile_warmup] = !profile_active && starts.zero? && stops.zero? }
yield_states.clear
setup_states.clear
teardown_states.clear
sampled = probe.sample_pairs
checks[:explicit_cpu_dependency_only] = loads == 1
checks[:same_30_pairs_with_only_builder_yields_profiled] = sampled == ordinary &&
  indices == (100...130).map { |index| [index, false] } && yield_states.length == 60 && yield_states.all?(&:last)
checks[:pair_setup_and_teardown_outside_profile] = setup_states == [false] * 30 && teardown_states == [false] * 30
checks[:start_stop_accumulate_until_one_results_call] = starts == 60 && stops == 60 && results_calls == 1 && !profile_active
summary = probe.instance_variable_get(:@cpu_profile)
checks[:numeric_summary] = summary.slice(:mode, :interval, :total_samples, :missed_samples, :gc_samples) ==
  { mode: 'cpu', interval: 1000, total_samples: 1500, missed_samples: 2, gc_samples: 3 }
checks[:bounded_source_frames] = summary[:top_frames].length == 30 && summary[:top_frames].all? do |frame|
  if frame[:kind] == 'native'
    frame.keys.sort == %i[kind name self_samples total_samples].sort &&
      ['Hash#each_pair', 'JSON::Ext::Generator::State#generate', 'OpenSSL::HMAC.digest'].include?(frame[:name])
  else
    frame.keys.sort == %i[file line name self_samples total_samples].sort &&
      frame[:file] == 'app/models/message.rb' && frame[:name] == 'Message#dispatch_create_events' && frame[:line] == 389
  end
end
checks[:self_sample_order] = summary[:top_frames].map { |frame| frame[:self_samples] } == [120, 80, 70, *(13...40).to_a.reverse]
checks[:native_marker_without_source_values] = summary[:top_frames].count { |frame| frame[:kind] == 'native' } == 3 &&
  summary[:top_frames].none? { |frame| frame[:file] == '<cfunc>' || frame.key?(:line) && frame[:line].nil? }
checks[:exclusive_coverage_including_drops_and_cap] = summary[:coverage] == {
  frame_self_samples: 1499, kept_self_samples: 1050, dropped_self_samples: 449,
  top_self_samples: 972, unattributed_samples: 1
}
checks[:mixed_native_source_ties_are_safe] = probe.cpu_profile_summary(report.merge(samples: 2, gc_samples: 0, frames: {
  1 => { name: 'Message#safe', file: '/app/app/models/message.rb', line: 1, samples: 1, total_samples: 1 },
  2 => { name: 'Hash#each_pair', file: '<cfunc>', line: nil, samples: 1, total_samples: 1 }
}))[:top_frames].length == 2
encoded = JSON.generate(summary)
checks[:no_raw_objects_or_private_labels] = !encoded.include?('private') && !encoded.include?('frames":{') &&
  !encoded.include?('0x') && !encoded.include?('edges') && !encoded.include?('raw')
checks[:invalid_report_rejected] = begin
  probe.cpu_profile_summary(report.merge(samples: 'private'))
  false
rescue NevidimyNativeYjitProbe::Failure => error
  error.message == 'cpu_profile_result'
end
checks[:unexpected_profile_mode_rejected] = begin
  probe.cpu_profile_summary(report.merge(mode: :wall))
  false
rescue NevidimyNativeYjitProbe::Failure => error
  error.message == 'cpu_profile_result'
end
checks[:malformed_self_samples_rejected] = begin
  probe.cpu_profile_summary(report.merge(frames: {
    1 => { name: 'Message#safe', file: '/app/app/models/message.rb', line: 1, samples: Float::INFINITY, total_samples: 2 }
  }))
  false
rescue NevidimyNativeYjitProbe::Failure => error
  error.message == 'cpu_profile_result'
end
checks[:impossible_coverage_rejected] = begin
  probe.cpu_profile_summary(report.merge(samples: 100))
  false
rescue NevidimyNativeYjitProbe::Failure => error
  error.message == 'cpu_profile_result'
end
failure = ArgumentError.new('neutral builder failure')
previous_stops, previous_results = stops, results_calls
checks[:builder_failure_stops_before_propagating_original_error] = begin
  probe.sample_pairs
  false
rescue ArgumentError => error
  error.equal?(failure) && !profile_active && stops == previous_stops + 1 && results_calls == previous_results
end
checks[:sample_phase_cleared_after_failure] = begin
  previous_starts = starts
  probe.metrics { :neutral_warmup }
  starts == previous_starts && !profile_active
end
failure = nil
reject_start = true
previous_stops, previous_results, previous_yields = stops, results_calls, yield_states.length
checks[:rejected_start_never_runs_builder_or_consumes_results] = begin
  probe.sample_pairs
  false
rescue NevidimyNativeYjitProbe::Failure => error
  error.message == 'cpu_profile_start' && !profile_active && stops == previous_stops &&
    results_calls == previous_results && yield_states.length == previous_yields
end
reject_start = false
missing = NevidimyNativeYjitProbe.new
missing.define_singleton_method(:load_cpu_profiler) { raise LoadError, 'private dependency path' }
checks[:missing_dependency_is_safe_error] = begin
  missing.configure_cpu_profile('cpu')
  false
rescue NevidimyNativeYjitProbe::Failure => error
  error.message == 'cpu_profile_dependency_missing'
end
checks[:ordinary_can_resume_without_profile] = begin
  probe.configure_cpu_profile(nil)
  indices.clear
  probe.sample_pairs == ordinary && indices.all? { |_, active| !active } && probe.instance_variable_get(:@cpu_profile).nil?
end
raise 'CPU profile contract failed' unless checks.values.all?
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
    assert len(report) == 25 and all(report.values())


def test_cpu_profile_scope_and_closed_dependency_path():
    source = (ROOT / "tests/timeweb_native_yjit_probe.rb").read_text()
    assert source.index("configure_cpu_profile(") < source.index("prepare_contact\n")
    assert source.index("WARMUP_PAIRS.times { |index| pair(index) }") < source.index("samples = sample_pairs")
    assert "@cpu_profiler.start(mode: :cpu, interval: 1000, raw: false" in source
    assert "@cpu_profiler.run(" not in source
    assert "NEVIDIMY_YJIT_PROFILE" in source
    assert "/run/nevidimy-stackprof" in source
    assert "stackprof-0.2.25/lib/stackprof.rb" in source
    assert "StackProf::VERSION == '0.2.25'" in source
    assert "$LOADED_FEATURES" in source and "File.realpath" in source


def test_cpu_profile_dependency_loader_rejects_unknown_artifacts():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the CPU dependency contracts")
    program = r"""
require ARGV.fetch(0)
root = NevidimyNativeYjitProbe::CPU_PROFILE_ROOT
entry = root + '/gems/stackprof-0.2.25/lib/stackprof.rb'
extension = root + '/extensions/x86_64-linux/3.4.0/stackprof-0.2.25'
native = extension + '/stackprof/stackprof.so'
original_realpath, original_directory, original_glob = File.method(:realpath), File.method(:directory?), Dir.method(:glob)
original_features, original_paths = $LOADED_FEATURES.dup, $LOAD_PATH.dup
original_root = ENV['NEVIDIMY_STACKPROF_ROOT']
checks = {}
begin
  scenarios = {
    valid: {}, missing: { missing: true }, wrong_version: { version: '0.2.24' },
    wrong_native_root: { native: '/tmp/stackprof/stackprof.so' },
    missing_native: { native: nil }, symlink_entry: { entry: '/tmp/stackprof.rb' },
    ambiguous_extension: { extensions: [extension, extension + '-other'] },
    missing_extension: { extensions: [] }, wrong_root: { root: '/tmp/other' },
    preloaded: { preloaded: true }
  }
  scenarios.each do |name, scenario|
    $LOADED_FEATURES.replace(original_features)
    Object.send(:remove_const, :StackProf) if Object.const_defined?(:StackProf)
    ENV['NEVIDIMY_STACKPROF_ROOT'] = scenario.fetch(:root, root)
    Object.const_set(:StackProf, Module.new) if scenario[:preloaded]
    File.singleton_class.send(:define_method, :realpath) do |path|
      raise Errno::ENOENT if scenario[:missing]
      path == entry ? scenario.fetch(:entry, path) : path
    end
    File.singleton_class.send(:define_method, :directory?) { |_| true }
    Dir.singleton_class.send(:define_method, :glob) { |_| scenario.fetch(:extensions, [extension]) }
    probe = NevidimyNativeYjitProbe.new
    required = []
    probe.define_singleton_method(:require) do |path|
      required << path
      mod = Module.new
      mod.const_set(:VERSION, scenario.fetch(:version, '0.2.25'))
      Object.const_set(:StackProf, mod)
      loaded = scenario.fetch(:native, native)
      $LOADED_FEATURES << loaded if loaded
      true
    end
    begin
      probe.configure_cpu_profile('cpu')
      checks[name] = name == :valid && required == [entry] && $LOAD_PATH == original_paths
    rescue NevidimyNativeYjitProbe::Failure => error
      expected = {
        missing: 'cpu_profile_dependency_missing', wrong_version: 'cpu_profile_dependency_contract',
        wrong_native_root: 'cpu_profile_dependency_contract', missing_native: 'cpu_profile_dependency_contract',
        symlink_entry: 'cpu_profile_dependency_path', ambiguous_extension: 'cpu_profile_extension_path',
        missing_extension: 'cpu_profile_extension_path', wrong_root: 'cpu_profile_dependency_root',
        preloaded: 'cpu_profile_already_loaded'
      }
      checks[name] = error.message == expected[name] && $LOAD_PATH == original_paths
    end
  end
ensure
  File.singleton_class.send(:define_method, :realpath, original_realpath)
  File.singleton_class.send(:define_method, :directory?, original_directory)
  Dir.singleton_class.send(:define_method, :glob, original_glob)
  $LOADED_FEATURES.replace(original_features)
  $LOAD_PATH.replace(original_paths)
  Object.send(:remove_const, :StackProf) if Object.const_defined?(:StackProf)
  original_root ? ENV['NEVIDIMY_STACKPROF_ROOT'] = original_root : ENV.delete('NEVIDIMY_STACKPROF_ROOT')
end
raise 'dependency contract failed' unless checks.values.all?
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
    assert len(report) == 10 and all(report.values())

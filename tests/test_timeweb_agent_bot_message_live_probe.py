"""Clone report validator and neutral local guards; no native services are started."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.chatwoot.contracts import (
    parse_conversation_changed,
    parse_message_created,
    parse_message_delivery_changed,
    parse_staff_message,
)

PROBE = Path(__file__).with_name("timeweb_agent_bot_message_live_probe.rb")
PREFIX = "AGENT_BOT_MESSAGE_PROBE="


def validate_report(report):
    """Use the real consumer on only the probe's fixed neutral whitelist."""
    if not isinstance(report, dict) or report.get("ok") is not True:
        raise ValueError("probe_failed")
    if report.get("consumer_scope") != "synthetic_neutral_fixtures_only":
        raise ValueError("consumer_scope")
    guards = report.get("guard_diagnostics", {})
    for name in (
        "candidate_installed", "callback_scope", "current_contract", "existing_projection",
        "self_filter_installed", "one_approved_recipient", "harmless_singleton_contract",
        "sample_current_contract", "existing_self_filter_predicate",
    ):
        if guards.get(name) is not True:
            raise ValueError("inactive_guard")
    invariants = report.get("invariants", {})
    for name in (
        "fixtures_rolled_back", "preexisting_core_rows_unchanged", "candidate_active",
        "existing_projection_active", "existing_self_filter_active",
    ):
        if invariants.get(name) is not True:
            raise ValueError("probe_invariant")
    if invariants.get("jobs_performed") != 0 or invariants.get("http_requests") != 0:
        raise ValueError("external_effect")
    results = report.get("results", {})
    if results.get("pairs") != 10 or any(results.get(name) is not True for name in (
        "job_ui_contract_equal", "mixed_native_equal", "single_recipient_compact_proven",
    )):
        raise ValueError("producer_comparison")
    cases = report.get("consumer_cases")
    if not isinstance(cases, list) or len(cases) != 1 or cases[0].get("name") != "single_internal":
        raise ValueError("consumer_cases")
    baseline, projected = cases[0].get("baseline"), cases[0].get("projected")
    for payload in (baseline, projected):
        if not isinstance(payload, dict) or set(payload) != {
            "event", "id", "message_type", "private", "content", "content_attributes",
            "conversation", "sender", "inbox",
        }:
            raise ValueError("consumer_whitelist")
        if (payload["event"] != "message_created" or payload["message_type"] != "incoming"
                or payload["private"] is not False
                or payload["content"] != "Neutral callback benchmark"
                or payload["content_attributes"] != {"nevidimy_telegram_callback_data": "menu:neutral"}
                or set(payload["conversation"]) != {"id", "channel"}
                or payload["conversation"]["channel"] != "Channel::Telegram"
                or set(payload["sender"]) != {"id"} or set(payload["inbox"]) != {"id"}):
            raise ValueError("non_neutral_fixture")
    expected = parse_message_created(baseline)
    if expected is None or parse_message_created(projected) != expected:
        raise ValueError("consumer_changed")
    for parser in (parse_staff_message, parse_message_delivery_changed, parse_conversation_changed):
        if parser(baseline) is not None or parser(projected) is not None:
            raise ValueError("other_consumer_changed")
    return {"ok": True, "consumer_equal": True, "other_consumers_ignored": True,
            "synthetic_scope_verified": True}


def neutral_report():
    payload = {"event": "message_created", "id": 11, "message_type": "incoming", "private": False,
               "content": "Neutral callback benchmark",
               "content_attributes": {"nevidimy_telegram_callback_data": "menu:neutral"},
               "conversation": {"id": 12, "channel": "Channel::Telegram"},
               "sender": {"id": 13}, "inbox": {"id": 14}}
    return {
        "ok": True, "consumer_scope": "synthetic_neutral_fixtures_only",
        "guard_diagnostics": dict.fromkeys((
            "candidate_installed", "callback_scope", "current_contract", "existing_projection",
            "self_filter_installed", "one_approved_recipient", "harmless_singleton_contract",
            "sample_current_contract", "existing_self_filter_predicate",
        ), True),
        "invariants": {**dict.fromkeys((
            "fixtures_rolled_back", "preexisting_core_rows_unchanged", "candidate_active",
            "existing_projection_active", "existing_self_filter_active",
        ), True), "jobs_performed": 0, "http_requests": 0},
        "results": {"pairs": 10, "job_ui_contract_equal": True, "mixed_native_equal": True,
                    "single_recipient_compact_proven": True},
        "consumer_cases": [{"name": "single_internal", "baseline": payload,
                            "projected": json.loads(json.dumps(payload))}],
    }


def test_actual_consumer_clone_report_and_fail_closed_validation():
    assert all(validate_report(neutral_report()).values())
    for section, key in (("guard_diagnostics", "current_contract"),
                         ("invariants", "fixtures_rolled_back"), ("results", "mixed_native_equal")):
        report = neutral_report()
        report[section][key] = False
        with pytest.raises(ValueError):
            validate_report(report)
    report = neutral_report()
    report["consumer_cases"][0]["projected"]["sender"]["id"] += 1
    with pytest.raises(ValueError, match="consumer_changed"):
        validate_report(report)
    report = neutral_report()
    report["consumer_cases"][0]["baseline"]["content"] = "not approved fixture text"
    with pytest.raises(ValueError, match="non_neutral_fixture"):
        validate_report(report)


def test_clone_probe_database_guards_and_no_execution_contract():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for clone probe contracts")
    program = r'''
require ARGV.fetch(0)
probe=NevidimyAgentBotMessageLiveProbe.new
valid={'expected_database'=>'nevidimy_cw419_012345abcdef','account_id'=>1,'inbox_id'=>2,
 'seed_conversation_id'=>3,'seed_run_id'=>'neutral_seed','run_id'=>'a'*40,'bot_id'=>4,
 'expected_yjit'=>false,'expected_candidate_sha256'=>'b'*64}
probe.configure(JSON.generate(valid));checks={valid_config:probe.config==valid}
checks[:bad_config]=[valid.merge('expected_database'=>'chatwoot'),valid.merge('expected_yjit'=>true),
 valid.merge('expected_candidate_sha256'=>nil),valid.merge('account_id'=>'1'),valid.merge('extra'=>1)].all? do |value|
 begin;probe.configure(JSON.generate(value));false;rescue NevidimyAgentBotMessageLiveProbe::Failure;true;end
end
probe.configure(JSON.generate(valid));tls={keyword:'sslmode',val:'verify-full'};encrypted=true
raw=Object.new;raw.define_singleton_method(:conninfo){[tls]};connection=Object.new
connection.define_singleton_method(:adapter_name){'PostgreSQL'}
connection.define_singleton_method(:open_transactions){0};connection.define_singleton_method(:raw_connection){raw}
connection.define_singleton_method(:select_value){|sql|sql.include?('current_database') ? valid['expected_database'] : encrypted}
probe.validate_database(connection,database:valid['expected_database']);checks[:database]=true
checks[:wrong_database]=begin;probe.validate_database(connection,database:'chatwoot');false;rescue NevidimyAgentBotMessageLiveProbe::Failure;true;end
tls[:val]='require'
checks[:tls]=begin;probe.validate_database(connection,database:valid['expected_database']);false;rescue NevidimyAgentBotMessageLiveProbe::Failure;true;end
tls[:val]='verify-full';encrypted=false
checks[:encrypted]=begin;probe.validate_database(connection,database:valid['expected_database']);false;rescue NevidimyAgentBotMessageLiveProbe::Failure;true;end
job=Object.new.extend(NevidimyAgentBotMessageLiveProbe::NoJobExecution)
checks[:no_jobs]=begin;job.perform_now;false;rescue NevidimyAgentBotMessageLiveProbe::Failure;true;end
http=Object.new.extend(NevidimyAgentBotMessageLiveProbe::NoNetwork)
checks[:no_http]=begin;http.request;false;rescue NevidimyAgentBotMessageLiveProbe::Failure;true;end
checks[:ten_pairs]=NevidimyAgentBotMessageLiveProbe::PAIRS==10
checks[:quantile]=probe.quantile([4,1,3,2],0.5)==2 && probe.quantile([4,1,3,2],0.95)==4
# Exercise the real sample method, including ensure, without Rails/DB/network.
require 'ostruct'
class Message
 def self.find(_);Object.new;end
end
module ActiveRecord
 class Base
  def self.connection;self;end
  def self.clear_query_cache;end
  def self.uncached;yield;end
 end
end
module ActiveSupport
 module Notifications
  class << self
   attr_accessor :subscriptions,:unsubscribed
   def monotonic_subscribe(*);self.subscriptions+=1;end
   def unsubscribe(value);unsubscribed<<value;end
  end
  self.subscriptions=0;self.unsubscribed=[]
 end
end
class ActionCableListener
 def self.instance;self;end
 def self.message_created(_);end
end
class AgentBotListener
 def self.instance;self;end
 def self.message_created(_);raise 'neutral_sample_failure' if @fail;end
 def self.fail=(value);@fail=value;end
end
module NevidimyAgentBotMessageProjection
 def self.current_contract?(*);true;end
 def self.payload(_);{};end
end
probe.instance_variable_set(:@adapter,OpenStruct.new(enqueued_jobs:[]))
probe.instance_variable_set(:@guard_diagnostics,{})
probe.define_singleton_method(:event){|message|message}
probe.define_singleton_method(:unpack_jobs){[{job:'AgentBots::WebhookJob',payload:{}}]}
probe.sample(:native);probe.sample(:candidate)
checks[:sample_success_unsubscribes]=ActiveSupport::Notifications.unsubscribed==[1,2]
AgentBotListener.fail=true
checks[:sample_failure_not_shadowed]=begin;probe.sample(:native);false;rescue RuntimeError=>error;error.message=='neutral_sample_failure';end
checks[:sample_failure_unsubscribes]=ActiveSupport::Notifications.unsubscribed==[1,2,3]
puts JSON.generate(checks)
'''
    result = subprocess.run([ruby, "-e", program, str(PROBE)], capture_output=True, text=True,
                            check=False, timeout=20)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout).values())
    source = PROBE.read_text()
    assert source.index("@stage = 'guards'; prove_guards") < source.index("@stage = 'benchmark'")
    for contract in ("raise ActiveRecord::Rollback", "candidate_contract_before_benchmark",
                     "harmless_singleton_contract", "candidate_fell_back", "Channel::Telegram",
                     "existing_self_filter_inactive", "existing_projection_inactive"):
        assert contract in source
    assert "payload: payload" not in source and "payload[:sql]" not in source


if __name__ == "__main__":
    import sys

    try:
        raw = sys.stdin.read()
        lines = [line[len(PREFIX):] for line in raw.splitlines() if line.startswith(PREFIX)]
        checked = validate_report(json.loads(lines[0] if len(lines) == 1 else raw))
    except (ValueError, TypeError, KeyError, AttributeError):
        checked = {"ok": False, "check": "consumer_report_invalid"}
    print("AGENT_BOT_MESSAGE_CONSUMER_CHECK=" + json.dumps(checked, separators=(",", ":")))
    raise SystemExit(0 if checked["ok"] else 1)

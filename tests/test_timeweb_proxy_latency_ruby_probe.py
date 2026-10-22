"""Standalone Ruby transport contracts with no network, app imports or credentials."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_proxy_latency_ruby_probe_is_read_only_and_fail_closed():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the standalone proxy probe contract")
    program = r"""
require 'securerandom'
require ARGV.fetch(0)
token = "8831692160:#{SecureRandom.hex(20)}"
probe = NevidimyProxyLatencyProbe.new
checks = {}
native = NevidimyProxyLatencyProbe::SingleConnectionHTTP.new('api.telegram.org', 443, 'telegram-proxy', 3128, nil, nil)
checks[:explicit_proxy_constructor] = native.address == 'api.telegram.org' && native.port == 443 &&
  native.proxy_address == 'telegram-proxy' && native.proxy_port == 3128 && !native.proxy_from_env? &&
  native.proxy_user.nil? && native.proxy_pass.nil?
mode = :ok
clients = []
class NeutralHttp
  attr_accessor :use_ssl, :verify_mode, :verify_hostname, :min_version,
                :max_retries, :open_timeout, :read_timeout, :write_timeout, :keep_alive_timeout
  attr_reader :requests, :starts, :finishes
  def initialize(mode, token)
    @mode, @token, @requests, @starts, @finishes = mode, token, [], 0, 0
  end
  def start
    @starts += 1
    @started = true
    self
  end
  def started?
    @started
  end
  def finish
    @finishes += 1
    @started = false
  end
  def request(request)
    @requests << request
    raise RuntimeError, @token if @mode == :unknown_error
    response = @mode == :redirect ? Net::HTTPFound.new('1.1', '302', 'Found') : Net::HTTPOK.new('1.1', '200', 'OK')
    body = JSON.generate('ok' => true, 'result' => {
      'id' => @mode == :wrong_identity ? 1 : 8831692160, 'is_bot' => true
    })
    response.define_singleton_method(:read_body) { |&block| block.call(body) }
    yield response
    response
  end
end
NevidimyProxyLatencyProbe::SingleConnectionHTTP.define_singleton_method(:new) do |*args|
  raise 'unexpected destination or proxy' unless args == ['api.telegram.org', 443, 'telegram-proxy', 3128, nil, nil]
  NeutralHttp.new(mode, token).tap { |http| clients << http }
end
report = probe.run(token: token, proxy: 'http://telegram-proxy:3128')
http = clients.last
checks[:twenty_sequential_read_only_calls] = report[:ok] && report[:samples].length == 20 &&
  http.requests.all? { |request| request.method == 'POST' && request.path == "/bot#{token}/getMe" }
checks[:alternating_json_bodies] = http.requests.map(&:body) == Array.new(10) { ['', '{}'] }.flatten &&
  http.requests.all? { |request| request['Content-Type'] == 'application/json' }
checks[:one_connection_lifecycle] = http.starts == 1 && http.finishes == 1
checks[:strict_tls_and_no_retry] = http.use_ssl && http.verify_mode == OpenSSL::SSL::VERIFY_PEER &&
  http.verify_hostname && http.min_version == OpenSSL::SSL::TLS1_2_VERSION && http.max_retries.zero?
checks[:metadata_only] = report.keys.sort == %i[connect_ms ok samples] &&
  report[:samples].all? { |sample| sample.keys.sort == %i[body_mode elapsed_ms identity_ok] && sample[:identity_ok] } &&
  !JSON.generate(report).include?(token)
before = clients.length
checks[:proxy_fail_closed] = ['', 'http://127.0.0.1:3128', 'http://telegram-proxy:3128/',
  'https://telegram-proxy:3128', 'http://user@telegram-proxy:3128'].all? do |proxy|
  result = probe.run(token: token, proxy: proxy)
  !result[:ok] && result[:error_type] == 'NevidimyProxyLatencyProbe::Rejected'
end && clients.length == before
checks[:token_path_fail_closed] = ['', '1:neutral', "#{token}\n", "#{token}/sendMessage"].all? do |value|
  !probe.run(token: value, proxy: 'http://telegram-proxy:3128')[:ok]
end && clients.length == before
mode = :redirect
result = probe.run(token: token, proxy: 'http://telegram-proxy:3128')
checks[:redirect_not_followed] = !result[:ok] && clients.last.requests.length == 1 && clients.last.finishes == 1
mode = :wrong_identity
result = probe.run(token: token, proxy: 'http://telegram-proxy:3128')
checks[:wrong_identity_stops] = !result[:ok] && result[:samples].first[:identity_ok] == false && clients.last.requests.length == 1
mode = :unknown_error
result = probe.run(token: token, proxy: 'http://telegram-proxy:3128')
checks[:unknown_errors_are_sanitized] = result[:error_type] == 'OtherError' && !JSON.generate(result).include?(token)
checks[:no_reconnect] = begin
  reconnect = NevidimyProxyLatencyProbe::SingleConnectionHTTP.allocate
  reconnect.instance_variable_set(:@connect_count, 1)
  reconnect.send(:connect)
  false
rescue NevidimyProxyLatencyProbe::Rejected
  true
end
raise 'neutral proxy contract failed' unless checks.values.all?
puts JSON.generate(checks)
"""
    result = subprocess.run(
        [ruby, "-e", program, str(ROOT / "tests/timeweb_proxy_latency_probe.rb")],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report) == 12 and all(report.values())

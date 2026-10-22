# Content-free deterministic fake Net::HTTP. Never opens a socket or boots Rails.
require 'json'
require 'net/http'
require 'openssl'
require 'ostruct'
require 'timeout'

module SafeFetch
  class UnsafeUrlError < StandardError; end
  class HttpError < StandardError; end
  class FileTooLargeError < StandardError; end
end

module Rails
  def self.application
    OpenStruct.new(reloader: Object.new.tap { |value| def value.to_prepare(&block); block.call; end })
  end
end

module Webhooks
  class Trigger
    attr_reader :fallback
    def initialize(url, kind = :agent_bot_webhook, secret: 'neutral-signing-key', timeout: 5, payload: {})
      @url, @webhook_type, @secret, @timeout, @payload = url, kind, secret, timeout, payload
    end
    def execute; perform_request; end
    private
    def perform_request; @fallback = true; end
    def webhook_timeout; @timeout; end
    def request_headers(body)
      { 'Content-Type' => 'application/json',
        'X-Chatwoot-Signature' => OpenSSL::HMAC.hexdigest('SHA256', @secret, body) }
    end
  end
end

class NeutralWebhookHttp
  attr_accessor :open_timeout, :read_timeout, :write_timeout, :max_retries, :use_ssl,
                :verify_mode, :verify_hostname, :ca_file, :min_version, :keep_alive_timeout,
                :failure, :on_request, :response_kind, :finish_failure, :start_failure
  attr_reader :requests, :starts, :finishes, :body_reads
  def initialize
    @requests, @starts, @finishes, @body_reads = [], 0, 0, 0
  end
  def use_ssl?; use_ssl == true; end
  def started?; @started == true; end
  def start
    @starts += 1
    raise start_failure if start_failure
    @started = true
    return self unless block_given?
    begin
      yield self
    ensure
      finish
    end
  end
  def finish
    @finishes += 1
    @started = false
    raise finish_failure if finish_failure
  end
  def request(request)
    raise 'concurrent connection access' if @busy
    @busy = true
    @requests << request
    raise failure if failure
    on_request&.call
    response = if response_kind == :redirect
                 Net::HTTPFound.new('1.1', '302', 'Found')
               else
                 Net::HTTPOK.new('1.1', '200', 'OK')
               end
    response['Connection'] = 'close' if response_kind == :close
    owner, large = self, response_kind == :oversize
    response.define_singleton_method(:read_body) do |&block|
      owner.instance_variable_set(:@body_reads, owner.body_reads + 1)
      block.call(large ? 'x' * 65_537 : '{}')
    end
    yield response
    response
  ensure
    @busy = false
  end
end

$webhook_connections = []
class << Net::HTTP
  def new(host, port, proxy)
    raise 'unexpected origin or proxy discovery' unless [host, port, proxy] == ['agent-bot', 8080, nil]
    NeutralWebhookHttp.new.tap do |http|
      http.start_failure = $webhook_start_failure
      $webhook_connections << http
    end
  end
end

require_relative '../deploy/timeweb/agent_webhook'

class << NevidimyInternalAgentWebhook
  def monotonic; $webhook_clock; end
end

def verify(value)
  raise 'neutral webhook keepalive contract failed' unless value
end

KEY = :nevidimy_internal_agent_webhook_https
APPROVED = 'https://agent-bot:8080/webhooks/chatwoot/agent/' + 'a' * 48
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = APPROVED
ENV['SSL_CERT_FILE'] = File.expand_path(__FILE__)
ENV.delete('NEVIDIMY_AGENT_BUTTON_SNAPSHOT')
$webhook_clock = 10.0

def deliver(**options)
  Webhooks::Trigger.new(ENV.fetch('NEVIDIMY_AGENT_WEBHOOK_URL'), **options).execute
end
def used
  $webhook_connections.select { |http| !http.requests.empty? }
end
def reset
  slot = Thread.current.thread_variable_get(KEY)
  slot[:http].finish if slot && slot[:http].started?
  Thread.current.thread_variable_set(KEY, nil)
  $webhook_connections.clear
  $webhook_clock = 10.0
  $webhook_start_failure = nil
  ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = APPROVED
  ENV['SSL_CERT_FILE'] = File.expand_path(__FILE__)
end

report = {}
first, second = deliver(payload: { number: 1 }), deliver(payload: { number: 2 })
verify(first.is_a?(Net::HTTPOK) && second.is_a?(Net::HTTPOK))
verify(used.length == 1 && used.first.requests.length == 2 && used.first.starts == 1 && used.first.started?)
http = used.first
verify(http.use_ssl? && http.verify_mode == OpenSSL::SSL::VERIFY_PEER && http.verify_hostname == true &&
       http.ca_file == ENV['SSL_CERT_FILE'] && http.min_version == OpenSSL::SSL::TLS1_2_VERSION &&
       http.max_retries.zero? && [http.open_timeout, http.read_timeout, http.write_timeout] == [5, 5, 5])
report[:verified_tls_session_reused_without_retries] = true

trigger = Webhooks::Trigger.new(APPROVED, secret: 'different-neutral-key', payload: { original: 1 })
trigger.define_singleton_method(:nevidimy_snapshot_body) { |_body| JSON.generate(final: 2) }
trigger.execute
request = http.requests.last
verify(request.body == JSON.generate(final: 2) && request['X-Chatwoot-Signature'] ==
       OpenSSL::HMAC.hexdigest('SHA256', 'different-neutral-key', request.body))
verify(trigger.instance_variable_get(:@payload) == { original: 1 })
report[:each_hmac_signs_actual_final_body_not_cached_headers] = true

$webhook_clock += 31
deliver
verify(http.finishes == 1 && used.length == 2)
http = used.last
9.times { $webhook_clock += 29; deliver }
$webhook_clock += 29
deliver
verify(http.finishes == 0)
$webhook_clock += 29
deliver
verify(http.finishes == 1)
report[:idle_and_absolute_lifetime_invalidate_the_slot] = true

http = used.last
deliver(timeout: 6)
verify(http.finishes == 1 && used.last.open_timeout == 6)
http = used.last
ENV['SSL_CERT_FILE'] = File.expand_path('../deploy/timeweb/agent_webhook.rb', __dir__)
deliver(timeout: 6)
verify(http.finishes == 1 && used.last.ca_file == ENV['SSL_CERT_FILE'])
http = used.last
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = APPROVED.sub('a' * 48, 'b' * 48)
deliver(timeout: 6)
verify(http.finishes == 1 && used.last.requests.last.path.end_with?('b' * 48))
report[:url_ca_and_timeout_changes_invalidate_configuration] = true

reset
deliver
http = used.last
class << File
  alias_method :original_stat_for_webhook_probe, :stat
  def stat(path)
    value = original_stat_for_webhook_probe(path)
    return value unless path == ENV['SSL_CERT_FILE']

    OpenStruct.new(dev: value.dev, ino: value.ino + 1, size: value.size,
                   mtime: value.mtime, ctime: value.ctime)
  end
end
begin
  deliver
  verify(http.finishes == 1 && used.length == 2)
ensure
  class << File
    alias_method :stat, :original_stat_for_webhook_probe
  end
end
report[:same_path_ca_file_identity_change_invalidates_tls] = true

reset
failure = OpenSSL::SSL::SSLError.new('neutral setup failure')
$webhook_start_failure = failure
begin
  deliver
  raise 'TLS setup failure swallowed'
rescue OpenSSL::SSL::SSLError => error
  verify(error.equal?(failure) && $webhook_connections.length == 1 &&
         $webhook_connections.first.starts == 1 && $webhook_connections.first.requests.empty? &&
         Thread.current.thread_variable_get(KEY).nil?)
end
report[:tls_setup_failure_is_evicted_without_transmission_or_retry] = true

reset
deliver
http, failure = used.last, IOError.new('neutral request failure')
http.failure = failure
http.finish_failure = RuntimeError.new('neutral cleanup failure')
begin
  deliver
  raise 'request failure swallowed'
rescue IOError => error
  verify(error.equal?(failure) && http.requests.length == 2 && http.finishes == 1)
end
verify(Thread.current.thread_variable_get(KEY).nil?)
deliver
verify(used.length == 2 && used.last.starts == 1)
report[:native_failure_evicts_without_retry_or_exception_replacement] = true

[:oversize, :redirect].each do |mode|
  reset
  deliver
  http = used.last
  http.response_kind = mode
  reads = http.body_reads
  begin
    deliver
    raise 'unsafe response accepted'
  rescue SafeFetch::FileTooLargeError, SafeFetch::HttpError
    verify(http.requests.length == 2 && http.finishes == 1 && Thread.current.thread_variable_get(KEY).nil?)
    verify(http.body_reads == reads) if mode == :redirect
  end
end
report[:non_success_redirect_and_body_limit_close_without_retry] = true

reset
deliver
http = used.last
http.response_kind = :close
verify(deliver.is_a?(Net::HTTPOK) && http.finishes == 1)
deliver
verify(used.length == 2)
report[:server_connection_close_is_not_retained] = true

reset
deliver
http = used.last
http.on_request = proc { http.on_request = nil; deliver(payload: { nested: 1 }) }
deliver
verify(used.length == 2 && used.last.finishes == 1 && used.first.started?)
deliver
verify(used.first.requests.length == 3 && used.last.requests.length == 1)
report[:reentrant_request_uses_fresh_connection_without_overwriting_busy_slot] = true

reset
entered, release = Queue.new, Queue.new
threads = 2.times.map do
  Thread.new do
    deliver
    own = Thread.current.thread_variable_get(KEY).fetch(:http)
    own.on_request = proc { entered << true; release.pop }
    deliver
  end
end
Timeout.timeout(2) { 2.times { entered.pop } }
2.times { release << true }
threads.each(&:value)
verify(used.length == 2 && used.all? { |connection| connection.requests.length == 2 && connection.starts == 1 })
report[:worker_threads_have_independent_bounded_slots_without_mutex_serialization] = true

reset
deliver
slot = Thread.current.thread_variable_get(KEY)
http = slot[:http]
slot[:pid] = -1
deliver
verify(http.finishes.zero? && used.length == 2 && Thread.current.thread_variable_get(KEY)[:pid] == Process.pid)
report[:pid_change_never_reuses_or_finishes_inherited_tls_socket] = true

reset
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = APPROVED.sub('https:', 'http:')
deliver
deliver
verify(used.length == 2 && used.all? { |connection| connection.finishes == 1 && !connection.use_ssl? })
verify(Thread.current.thread_variable_get(KEY).nil?)
report[:plain_http_keeps_its_original_per_call_lifecycle] = true

reset
[APPROVED + '?unexpected=1', APPROVED.sub('agent-bot', '127.0.0.1'), APPROVED.sub('a' * 48, 'c' * 48)].each do |url|
  trigger = Webhooks::Trigger.new(url)
  trigger.execute
  verify(trigger.fallback)
end
trigger = Webhooks::Trigger.new(APPROVED, :account_webhook)
trigger.execute
verify(trigger.fallback && $webhook_connections.empty?)
begin
  Webhooks::Trigger.new(APPROVED, secret: nil).execute
  raise 'unsigned internal webhook accepted'
rescue SafeFetch::UnsafeUrlError
  verify($webhook_connections.empty?)
end
report[:exact_url_webhook_type_and_signing_allowlist_preserved] = true

reset
deliver
http = used.last
$webhook_clock = nil
deliver
verify(http.finishes == 1 && used.last.finishes == 1 && Thread.current.thread_variable_get(KEY).nil?)
report[:unavailable_clock_falls_back_to_native_per_call_delivery] = true

reset
deliver
http = used.last
ENV['SSL_CERT_FILE'] = File.expand_path('nonexistent-neutral-ca', __dir__)
deliver
verify(http.finishes == 1 && used.last.finishes == 1 && Thread.current.thread_variable_get(KEY).nil?)
report[:missing_ca_metadata_cannot_reuse_a_previously_verified_session] = true

reset
puts JSON.generate(report)

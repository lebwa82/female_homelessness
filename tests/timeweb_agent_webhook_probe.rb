require 'json'
require 'net/http'
require 'ostruct'

module SafeFetch
  class UnsafeUrlError < StandardError; end
  class HttpError < StandardError; end
  class FileTooLargeError < StandardError; end
end

module Rails
  def self.application
    @application ||= OpenStruct.new(reloader: Object.new.tap do |value|
      def value.to_prepare(&block)
        block.call
      end
    end)
  end
end

module Webhooks
  class Trigger
    attr_reader :fallback

    def initialize(url, kind = :agent_bot_webhook, secret = 'test-only-signing-secret')
      @url, @webhook_type, @secret = url, kind, secret
      @payload = { event: 'integration_probe' }
    end

    def execute
      perform_request
    end

    private

    def perform_request
      @fallback = true
    end

    def request_headers(body)
      raise 'body changed' unless body == @payload.to_json
      { 'X-Chatwoot-Signature' => 'sha256=test-only', 'Content-Type' => 'application/json' }
    end

    def webhook_timeout
      5
    end
  end
end

require_relative '../deploy/timeweb/agent_webhook'

class FakeHttp
  attr_accessor :open_timeout, :read_timeout, :write_timeout, :max_retries,
                :use_ssl, :verify_mode, :ca_file, :min_version
  attr_reader :requests

  def initialize
    @requests = []
  end

  def start
    yield self
  end

  def request(request)
    @requests << request
    response = $probe_response
    def response.read_body
      yield 'ok'
    end
    yield response
  end
end

class << Net::HTTP
  alias_method :original_new_for_probe, :new

  def new(host, port, proxy)
    raise 'unexpected destination or proxy' unless [host, port, proxy] == ['agent-bot', 8080, nil]
    $probe_http = FakeHttp.new
  end
end

def verify(condition)
  raise 'probe assertion failed' unless condition
end

approved = 'http://agent-bot:8080/webhooks/chatwoot/agent/' + 'a' * 48
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved
$probe_response = Net::HTTPOK.new('1.1', '200', 'OK')
trigger = Webhooks::Trigger.new(approved)
trigger.execute
verify(!trigger.fallback && $probe_http.requests.size == 1 && $probe_http.max_retries == 0)
verify($probe_http.requests.first.path == '/webhooks/chatwoot/agent/' + 'a' * 48)
verify($probe_http.requests.first['X-Chatwoot-Signature'] == 'sha256=test-only')

https_approved = approved.sub('http:', 'https:')
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = https_approved
ENV['SSL_CERT_FILE'] = '/run/tls/ca-bundle.pem'
Webhooks::Trigger.new(https_approved).execute
verify($probe_http.use_ssl && $probe_http.verify_mode == OpenSSL::SSL::VERIFY_PEER)
verify($probe_http.ca_file == '/run/tls/ca-bundle.pem')
verify($probe_http.min_version == OpenSSL::SSL::TLS1_2_VERSION)
ENV.delete('SSL_CERT_FILE')
begin
  Webhooks::Trigger.new(https_approved).execute
  raise 'missing CA accepted'
rescue KeyError
end
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved

['http://127.0.0.1:8080/', approved + '?next=private', approved.sub('a' * 48, 'b' * 48)].each do |url|
  trigger = Webhooks::Trigger.new(url)
  trigger.execute
  verify(trigger.fallback)
end
trigger = Webhooks::Trigger.new(approved, :api_inbox_webhook)
trigger.execute
verify(trigger.fallback)
ENV.delete('NEVIDIMY_AGENT_WEBHOOK_URL')
trigger = Webhooks::Trigger.new(approved)
trigger.execute
verify(trigger.fallback)
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved

['http://127.0.0.1:8080/', approved.sub('agent-bot', 'user:password@agent-bot'), approved + '?next=private'].each do |url|
  ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = url
  trigger = Webhooks::Trigger.new(url)
  trigger.execute
  verify(trigger.fallback)
end
ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved

begin
  Webhooks::Trigger.new(approved, :agent_bot_webhook, nil).execute
  raise 'missing secret accepted'
rescue SafeFetch::UnsafeUrlError
end

$probe_response = Net::HTTPFound.new('1.1', '302', 'Found')
$probe_response['Location'] = 'http://169.254.169.254/'
begin
  Webhooks::Trigger.new(approved).execute
  raise 'redirect accepted'
rescue SafeFetch::HttpError
  verify($probe_http.requests.size == 1)
end
verify(ENV['SAFE_FETCH_ALLOW_PRIVATE_NETWORK'].nil?)
puts JSON.generate(exact_url_only: true, signing_required: true, no_proxy: true,
                   no_redirects: true, other_urls_use_safe_fetch: true)

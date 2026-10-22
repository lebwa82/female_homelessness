# Neutral loopback only: real Net::HTTP/OpenSSL, no Rails boot or external service.
require 'json'
require 'net/http'
require 'openssl'
require 'ostruct'
require 'socket'
require 'tmpdir'
require 'timeout'

def verify(condition)
  raise 'neutral webhook TLS contract failed' unless condition
end

def certificate(name, serial, issuer = nil)
  key = OpenSSL::PKey::RSA.new(2048)
  cert = OpenSSL::X509::Certificate.new
  cert.version, cert.serial = 2, serial
  cert.subject = OpenSSL::X509::Name.parse("/CN=#{name}")
  cert.issuer = issuer ? issuer.first.subject : cert.subject
  cert.public_key = key.public_key
  cert.not_before, cert.not_after = Time.now - 60, Time.now + 3600
  factory = OpenSSL::X509::ExtensionFactory.new
  factory.subject_certificate, factory.issuer_certificate = cert, issuer ? issuer.first : cert
  cert.add_extension(factory.create_extension('basicConstraints', issuer ? 'CA:FALSE' : 'CA:TRUE', true))
  cert.add_extension(factory.create_extension('keyUsage', issuer ? 'digitalSignature,keyEncipherment' : 'keyCertSign,cRLSign', true))
  cert.add_extension(factory.create_extension('subjectKeyIdentifier', 'hash'))
  if issuer
    cert.add_extension(factory.create_extension('extendedKeyUsage', 'serverAuth'))
    cert.add_extension(factory.create_extension('subjectAltName', "DNS:#{name}"))
  end
  cert.sign(issuer ? issuer.last : key, OpenSSL::Digest::SHA256.new)
  [cert, key]
end

class NeutralWebhookTlsEndpoint
  attr_reader :port, :connections, :requests, :versions, :server_names, :valid_signatures, :bodies
  attr_accessor :mode

  def initialize(identity, mode = nil)
    @context = OpenSSL::SSL::SSLContext.new
    @context.cert, @context.key = identity
    @context.min_version = @context.max_version = OpenSSL::SSL::TLS1_2_VERSION
    @connections, @requests, @valid_signatures = 0, 0, 0
    @versions, @server_names, @bodies, @sockets, @errors = [], [], [], [], []
    @closed, @mode = Queue.new, mode
    @context.servername_cb = proc { |_socket, name| @server_names << name; nil }
    @listener = TCPServer.new('127.0.0.1', 0)
    @port = @listener.addr[1]
    @worker = Thread.new do
      loop do
        socket = @listener.accept
        @sockets << socket
        serve(socket)
      end
    rescue IOError, Errno::EBADF
      nil
    rescue StandardError => error
      @errors << error
    end
    @worker.report_on_exception = false
  end

  def stop
    @listener.close unless @listener.closed?
    @sockets.each { |socket| socket.close unless socket.closed? }
    verify(@worker.join(3))
    raise @errors.first unless @errors.empty?
  end

  def wait_closed
    Timeout.timeout(3) { @closed.pop }
  end

  private

  def serve(socket)
    tls = OpenSSL::SSL::SSLSocket.new(socket, @context)
    tls.sync_close = true
    tls.accept
    @connections += 1
    @versions << tls.ssl_version
    while (line = tls.gets)
      verify(line == "POST /webhooks/chatwoot/agent/#{'a' * 48} HTTP/1.1\r\n")
      headers = {}
      while (line = tls.gets) && line != "\r\n"
        name, value = line.split(':', 2)
        headers[name.downcase] = value.strip
      end
      length = Integer(headers.fetch('content-length'))
      verify(length.positive? && length <= 2048)
      body = tls.read(length)
      verify(body && body.bytesize == length)
      @bodies << body
      signature = OpenSSL::HMAC.hexdigest('SHA256', 'neutral-signing-key', body)
      @valid_signatures += 1 if headers['x-chatwoot-signature'] == signature
      @requests += 1
      break if mode == :drop_response

      tls.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
      break if mode == :close_idle
    end
  rescue OpenSSL::SSL::SSLError, IOError, SystemCallError
    nil # Rejected handshakes and closing a retained idle connection are expected.
  ensure
    tls&.close rescue nil
    socket.close rescue nil
    @closed << true
  end
end

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
    def initialize(number)
      @url, @webhook_type, @secret = ENV.fetch('NEVIDIMY_AGENT_WEBHOOK_URL'), :agent_bot_webhook, 'neutral-signing-key'
      @payload = { sequence: number }
    end
    def execute; perform_request; end
    private
    def perform_request; raise 'approved origin incorrectly fell back'; end
    def webhook_timeout; 2; end
    def request_headers(body)
      { 'Content-Type' => 'application/json',
        'X-Chatwoot-Signature' => OpenSSL::HMAC.hexdigest('SHA256', @secret, body) }
    end
  end
end

# Keep address=agent-bot for SNI/hostname verification. Only the TCP destination
# and ephemeral listening port differ; all session/request/TLS methods are native.
class << Net::HTTP
  alias_method :new_before_neutral_webhook_tls_probe, :new
  def new(address, port, proxy)
    verify([address, port, proxy] == ['agent-bot', 8080, nil])
    new_before_neutral_webhook_tls_probe(address, $webhook_tls_endpoint.port, nil).tap do |http|
      http.ipaddr = '127.0.0.1'
    end
  end
end

require_relative '../deploy/timeweb/agent_webhook'

def close_slot
  slot = Thread.current.thread_variable_get(NevidimyInternalAgentWebhook::HTTPS_SLOT)
  NevidimyInternalAgentWebhook.close(slot[:http]) if slot
  Thread.current.thread_variable_set(NevidimyInternalAgentWebhook::HTTPS_SLOT, nil)
end

def rejected_before_http(endpoint)
  begin
    Webhooks::Trigger.new(1).execute
    raise 'invalid TLS peer accepted'
  rescue OpenSSL::SSL::SSLError
    verify(endpoint.requests.zero?)
    verify(Thread.current.thread_variable_get(NevidimyInternalAgentWebhook::HTTPS_SLOT).nil?)
  end
end

phase, endpoints, report = 1, [], {}
begin
  Timeout.timeout(25) do
    Dir.mktmpdir('neutral-agent-webhook-tls-') do |directory|
      ca = certificate('Neutral Webhook CA', 1)
      unrelated_ca = certificate('Neutral Other CA', 2)
      trusted_file = File.join(directory, 'ca.pem')
      File.write(trusted_file, ca.first.to_pem, mode: 'w', perm: 0o600)
      ENV['SSL_CERT_FILE'] = trusted_file
      ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = 'https://agent-bot:8080/webhooks/chatwoot/agent/' + 'a' * 48
      ENV.delete('NEVIDIMY_AGENT_BUTTON_SNAPSHOT')

      phase = 2
      endpoint = NeutralWebhookTlsEndpoint.new(certificate('agent-bot', 3, ca))
      endpoints << endpoint
      $webhook_tls_endpoint = endpoint
      2.times { |number| verify(Webhooks::Trigger.new(number).execute.is_a?(Net::HTTPSuccess)) }
      verify(endpoint.connections == 1 && endpoint.requests == 2 && endpoint.valid_signatures == 2)
      verify(endpoint.bodies == [JSON.generate(sequence: 0), JSON.generate(sequence: 1)])
      verify(endpoint.versions == ['TLSv1.2'] && endpoint.server_names == ['agent-bot'])
      report[:verified_tls12_hostname_and_fresh_hmac_reuse_one_tcp] = 1

      phase = 3
      replacement = File.join(directory, 'next-ca.pem')
      File.write(replacement, ca.first.to_pem, mode: 'w', perm: 0o600)
      verify(File.stat(replacement).ino != File.stat(trusted_file).ino)
      File.rename(replacement, trusted_file)
      verify(Webhooks::Trigger.new(2).execute.is_a?(Net::HTTPSuccess))
      verify(endpoint.connections == 2 && endpoint.requests == 3 && endpoint.valid_signatures == 3)
      verify(endpoint.versions == ['TLSv1.2', 'TLSv1.2'])
      report[:same_path_ca_identity_replacement_forces_new_tls_handshake] = 1
      close_slot
      endpoint.stop
      endpoints.delete(endpoint)

      phase = 4
      endpoint = NeutralWebhookTlsEndpoint.new(certificate('agent-bot', 4, ca))
      endpoints << endpoint
      $webhook_tls_endpoint = endpoint
      File.write(trusted_file, unrelated_ca.first.to_pem, mode: 'w', perm: 0o600)
      rejected_before_http(endpoint)
      endpoint.stop
      endpoints.delete(endpoint)
      report[:untrusted_ca_rejected_before_any_http_request] = 1

      phase = 5
      endpoint = NeutralWebhookTlsEndpoint.new(certificate('not-agent-bot.invalid', 5, ca))
      endpoints << endpoint
      $webhook_tls_endpoint = endpoint
      File.write(trusted_file, ca.first.to_pem, mode: 'w', perm: 0o600)
      rejected_before_http(endpoint)
      endpoint.stop
      endpoints.delete(endpoint)
      report[:wrong_san_rejected_before_any_http_request] = 1

      phase = 6
      endpoint = NeutralWebhookTlsEndpoint.new(certificate('agent-bot', 6, ca), :close_idle)
      endpoints << endpoint
      $webhook_tls_endpoint = endpoint
      verify(Webhooks::Trigger.new(6).execute.is_a?(Net::HTTPSuccess))
      endpoint.wait_closed # Ensure the old peer closed before the next POST starts.
      verify(Webhooks::Trigger.new(7).execute.is_a?(Net::HTTPSuccess))
      verify(endpoint.connections == 2 && endpoint.requests == 2 && endpoint.valid_signatures == 2)
      verify(endpoint.bodies == [JSON.generate(sequence: 6), JSON.generate(sequence: 7)])
      close_slot
      endpoint.stop
      endpoints.delete(endpoint)
      report[:closed_idle_socket_reconnects_before_next_post_without_replaying_it] = 1

      phase = 7
      endpoint = NeutralWebhookTlsEndpoint.new(certificate('agent-bot', 7, ca), :drop_response)
      endpoints << endpoint
      $webhook_tls_endpoint = endpoint
      failure = nil
      begin
        Webhooks::Trigger.new(8).execute
      rescue EOFError, IOError, OpenSSL::SSL::SSLError, Errno::ECONNRESET => error
        failure = error
      end
      verify(failure && endpoint.connections == 1 && endpoint.requests == 1 && endpoint.valid_signatures == 1)
      verify(Thread.current.thread_variable_get(NevidimyInternalAgentWebhook::HTTPS_SLOT).nil?)
      endpoint.stop
      endpoints.delete(endpoint)
      report[:accepted_post_then_response_eof_is_evicted_and_never_automatically_retried] = 1
    end
  end
  puts JSON.generate(report)
rescue StandardError => error
  allowed_files = [File.expand_path(__FILE__), File.expand_path('../deploy/timeweb/agent_webhook.rb', __dir__)]
  frames = (error.backtrace_locations || []).select { |frame| allowed_files.include?(File.expand_path(frame.path)) }
  error_type = error.class.name.to_s
  error_type = 'StandardError' unless error_type.match?(/\A[A-Za-z][A-Za-z0-9_:]{0,96}\z/)
  puts JSON.generate(ok: 0, phase: phase, error_type: error_type,
                     frames: frames.first(3).map { |frame| { file: File.basename(frame.path), line: frame.lineno } })
  exit 1
ensure
  close_slot
  endpoints.each(&:stop)
end

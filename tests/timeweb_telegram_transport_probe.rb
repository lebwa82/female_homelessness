# Synthetic loopback CONNECT proxy and TLS endpoint. Never contacts Telegram.
require 'json'
require 'net/http'
require 'openssl'
require 'ostruct'
require 'socket'
require 'timeout'

def verify(value, message)
  raise message unless value
end

def certificate(host)
  key = OpenSSL::PKey::RSA.new(2048)
  cert = OpenSSL::X509::Certificate.new
  cert.version = 2
  cert.serial = 1
  cert.subject = cert.issuer = OpenSSL::X509::Name.parse("/CN=#{host}")
  cert.public_key = key.public_key
  cert.not_before = Time.now - 60
  cert.not_after = Time.now + 3600
  factory = OpenSSL::X509::ExtensionFactory.new
  factory.subject_certificate = factory.issuer_certificate = cert
  cert.add_extension(factory.create_extension('basicConstraints', 'CA:TRUE', true))
  cert.add_extension(factory.create_extension('subjectAltName', "DNS:#{host}"))
  cert.sign(key, OpenSSL::Digest::SHA256.new)
  [cert, key]
end

class Endpoint
  attr_reader :port, :cert, :requests, :connections, :arrivals, :releases, :maximum_active
  attr_accessor :mode

  def initialize(host = 'api.telegram.org')
    @cert, key = certificate(host)
    @context = OpenSSL::SSL::SSLContext.new
    @context.cert, @context.key = @cert, key
    @listener = TCPServer.new('127.0.0.1', 0)
    @port = @listener.addr[1]
    @mutex = Mutex.new
    @requests, @connections, @active, @maximum_active = 0, 0, 0, 0
    @arrivals, @releases = Queue.new, Queue.new
    @sockets, @workers = [], []
    @acceptor = Thread.new do
      loop do
        socket = @listener.accept
        @sockets << socket
        @workers << Thread.new(socket) { |client| serve(client) }
      end
    rescue IOError, Errno::EBADF
      nil
    end
  end

  def store
    @store ||= OpenSSL::X509::Store.new.tap { |store| store.add_cert(cert) }
  end

  def stop
    @listener.close
    @sockets.each { |socket| socket.close rescue nil }
    @workers.each { |worker| worker.join(2) }
    @acceptor.join(2)
  end

  private

  def headers(socket)
    first = socket.gets
    return unless first

    result = {}
    while (line = socket.gets) && line != "\r\n"
      key, value = line.split(':', 2)
      result[key.downcase] = value.strip
    end
    [first, result]
  end

  def serve(socket)
    connect, = headers(socket)
    verify(connect == "CONNECT api.telegram.org:443 HTTP/1.1\r\n", 'unexpected CONNECT target')
    socket.write("HTTP/1.1 200 Connection established\r\n\r\n")
    tls = OpenSSL::SSL::SSLSocket.new(socket, @context)
    tls.sync_close = true
    tls.accept
    @mutex.synchronize { @connections += 1 }
    while (request = headers(tls))
      first, values = request
      verify(first.start_with?('POST /bot123:synthetic/sendMessage '), 'unexpected native request')
      body = tls.read(values.fetch('content-length').to_i)
      verify(body.include?('text=synthetic'), 'native request body changed')
      @mutex.synchronize do
        @requests += 1
        @active += 1
        @maximum_active = [@maximum_active, @active].max
      end
      @arrivals << true
      @releases.pop if mode == :hold
      break if mode == :drop

      tls.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")
      @mutex.synchronize { @active -= 1 }
    end
  rescue OpenSSL::SSL::SSLError, IOError, SystemCallError
    nil
  ensure
    tls.close rescue nil
    socket.close rescue nil
  end
end

module Rails
  def self.application
    OpenStruct.new(reloader: Object.new.tap do |value|
      def value.to_prepare(&block)
        block.call
      end
    end)
  end
end

# Only HTTParty's factory/request seam is stubbed. Sessions, CONNECT, TLS,
# hostname verification, POST transmission, and socket reuse are real Net::HTTP.
module HTTParty
  NATIVE = Object.new

  class ConnectionAdapter
    def self.call(uri, options)
      endpoint = HTTParty.endpoint
      http = Net::HTTP.new(uri.host, uri.port, '127.0.0.1', endpoint.port,
                           options[:http_proxyuser], options[:http_proxypass])
      http.use_ssl = true
      http.verify_mode = options[:verify] == false ? OpenSSL::SSL::VERIFY_NONE : OpenSSL::SSL::VERIFY_PEER
      http.cert_store = options.fetch(:cert_store, endpoint.store)
      http.open_timeout = http.read_timeout = http.write_timeout = 2
      http.read_timeout = options[:read_timeout] if options[:read_timeout]
      HTTParty.candidates << http
      http
    end
  end

  class << self
    attr_accessor :endpoint, :last_options

    def candidates
      @candidates ||= []
    end

    def post(url, options = {}, &block)
      self.last_options = options
      return NATIVE unless options[:connection_adapter]

      uri = URI.parse(url)
      request = Net::HTTP::Post.new(uri.request_uri)
      request.set_form_data(options.fetch(:body))
      options[:connection_adapter].call(uri, options).request(request, &block)
    end
  end
end

module Channel
  class Telegram
    attr_accessor :destination, :extra_options, :fail_before_post

    def initialize
      @destination = 'https://api.telegram.org/bot123:synthetic/sendMessage'
      @extra_options = {}
    end

    private

    def message_request(text: 'synthetic')
      raise ArgumentError, 'synthetic native failure' if fail_before_post

      HTTParty.post(destination, { body: { chat_id: 123, text: text, parse_mode: 'HTML' } }.merge(extra_options))
    end
  end
end

require_relative '../deploy/timeweb/telegram_transport'

def send_text(channel)
  channel.send(:message_request)
end

def expire_idle(pool, age)
  pool.instance_variable_get(:@idle).each { |entry| entry[2] -= age }
end

def drain(queue)
  loop { queue.pop(true) }
rescue ThreadError
  nil
end

report = {}
endpoints = []
begin
  endpoint = Endpoint.new
  endpoints << endpoint
  HTTParty.endpoint = endpoint
  channel = Channel::Telegram.new
  first, second = send_text(channel), send_text(channel)
  verify(first.is_a?(Net::HTTPOK) && second.body == '{"ok":true}', 'native response changed')
  verify(endpoint.connections == 1 && endpoint.requests == 2, 'verified TLS socket was not reused')
  verify(HTTParty.last_options[:body] == { chat_id: 123, text: 'synthetic', parse_mode: 'HTML' }, 'native body changed')
  verify(HTTParty.last_options[:follow_redirects] == false, 'POST redirects could replay a request')
  report['verified_tls_socket_reuse_and_native_contract'] = true

  endpoint.mode = :hold
  2.times { endpoint.arrivals.pop }
  threads = 6.times.map { Thread.new { send_text(Channel::Telegram.new) } }
  Timeout.timeout(5) { 4.times { endpoint.arrivals.pop } }
  verify(endpoint.connections == 4 && endpoint.maximum_active == 4, 'pool capacity was not enforced')
  6.times { endpoint.releases << true }
  responses = threads.map(&:value)
  endpoint.mode = nil
  verify(responses.all? { |response| response.is_a?(Net::HTTPOK) }, 'parallel native requests failed')
  verify(endpoint.connections == 4, 'parallel requests shared or exceeded bounded sessions')
  report['exclusive_parallel_leases_with_bounded_pool'] = true

  drain(endpoint.arrivals)
  endpoint.mode = :hold
  holders = 4.times.map { Thread.new { send_text(Channel::Telegram.new) } }
  Timeout.timeout(5) { 4.times { endpoint.arrivals.pop } }
  original_checkout = NevidimyTelegramTransport::CHECKOUT_SECONDS
  NevidimyTelegramTransport.send(:remove_const, :CHECKOUT_SECONDS)
  NevidimyTelegramTransport.const_set(:CHECKOUT_SECONDS, 0.01)
  before = endpoint.requests
  begin
    send_text(channel)
    raise 'exhausted pool unexpectedly accepted request'
  rescue Timeout::Error => error
    verify(error.message == 'Telegram connection checkout timed out', 'unexpected checkout error')
    verify(endpoint.requests == before, 'checkout timeout transmitted a POST')
  ensure
    NevidimyTelegramTransport.send(:remove_const, :CHECKOUT_SECONDS)
    NevidimyTelegramTransport.const_set(:CHECKOUT_SECONDS, original_checkout)
    4.times { endpoint.releases << true }
    holders.each(&:value)
    endpoint.mode = nil
  end
  report['checkout_timeout_is_bounded_without_transmission'] = true

  candidate = HTTParty.candidates.last
  verify(candidate.use_ssl? && candidate.verify_mode == OpenSSL::SSL::VERIFY_PEER, 'certificate verification disabled')
  verify(candidate.max_retries.zero? && candidate.verify_callback.nil?, 'unsafe retry or TLS callback')
  verify(!candidate.respond_to?(:verify_hostname) || candidate.verify_hostname, 'hostname verification disabled')
  report['verified_configuration_and_zero_transport_retries'] = true

  pool = NevidimyTelegramTransport::POOL
  expire_idle(pool, NevidimyTelegramTransport::IDLE_SECONDS + 1)
  before = endpoint.connections
  send_text(channel)
  verify(endpoint.connections == before + 1, 'idle sessions were not reaped')
  pool.instance_variable_get(:@created_at).transform_values! { |born| born - NevidimyTelegramTransport::LIFETIME_SECONDS - 1 }
  send_text(channel)
  verify(endpoint.connections == before + 2, 'connection lifetime was not bounded')
  report['idle_and_lifetime_expiry'] = true

  before = endpoint.requests
  endpoint.mode = :drop
  begin
    send_text(channel)
    raise 'ambiguous POST unexpectedly succeeded'
  rescue EOFError, IOError, Net::ReadTimeout, Errno::ECONNRESET
    verify(endpoint.requests == before + 1, 'ambiguous POST was retried')
  end
  endpoint.mode = nil
  before = endpoint.connections
  send_text(channel)
  verify(endpoint.connections == before + 1, 'failed session was reused')
  report['ambiguous_post_not_retried_and_failed_socket_discarded'] = true

  before = endpoint.connections
  channel.extra_options = { read_timeout: 1 }
  send_text(channel)
  verify(endpoint.connections == before + 1, 'changed transport settings reused old session')
  channel.extra_options = {}
  report['transport_configuration_isolation'] = true

  channel.extra_options = { http_proxyuser: 'synthetic-user', http_proxypass: 'synthetic-password' }
  before = endpoint.connections
  send_text(channel)
  verify(endpoint.connections == before + 1, 'changed proxy identity reused old session')
  keys = pool.instance_variable_get(:@idle).map { |entry| entry[1] }
  verify(keys.all? { |key| /\A[0-9a-f]{64}\z/.match?(key) }, 'pool exposed transport credentials in keys')
  channel.extra_options = {}
  report['proxy_identity_isolation_without_credential_keys'] = true

  [Endpoint.new('wrong.example'), Endpoint.new].each_with_index do |invalid, index|
    endpoints << invalid
    HTTParty.endpoint = invalid
    channel.extra_options = { verify: false }
    channel.extra_options[:cert_store] = OpenSSL::X509::Store.new if index == 1
    begin
      send_text(channel)
      raise 'invalid TLS certificate unexpectedly accepted'
    rescue OpenSSL::SSL::SSLError
      verify(invalid.requests.zero?, 'request transmitted before verification')
    end
  end
  report['untrusted_and_wrong_hostname_certificates_rejected'] = true

  channel.destination = 'https://unrelated.example/path'
  verify(send_text(channel).equal?(HTTParty::NATIVE), 'unrelated destination was intercepted')
  verify(HTTParty.post('https://api.telegram.org/bot123:synthetic/sendMessage').equal?(HTTParty::NATIVE), 'unscoped request was intercepted')
  %w[http://api.telegram.org/bot123:synthetic/sendMessage
     https://api.telegram.org:444/bot123:synthetic/sendMessage
     https://api.telegram.org.evil.example/bot123:synthetic/sendMessage
     https://user@api.telegram.org/bot123:synthetic/sendMessage
     https://api.telegram.org/bot123:synthetic/sendPhoto].each do |url|
    verify(!NevidimyTelegramTransport.destination?(url), 'unsafe destination accepted')
    begin
      NevidimyTelegramTransport::Adapter.call(URI.parse(url), {})
      raise 'specialized adapter accepted unsafe destination'
    rescue ArgumentError => error
      verify(error.message == 'Telegram transport destination is not allowed', 'unsafe destination reached native adapter')
    end
  end
  channel.fail_before_post = true
  begin
    send_text(channel)
  rescue ArgumentError
    verify(Thread.current[NevidimyTelegramTransport::SCOPE_KEY].nil?, 'request scope leaked on failure')
  end
  report['unrelated_pass_through_fixed_origin_and_scope_cleanup'] = true
  puts JSON.generate(report)
ensure
  pool&.instance_variable_get(:@connections)&.each { |connection| connection.finish if connection.started? }
  endpoints.each(&:stop)
end

# Reuse verified Telegram text connections without replacing Chatwoot's request body,
# response parsing, or error handling. No request is retried by this transport.
require 'digest'
require 'json'
require 'net/http'
require 'openssl'
require 'thread'
require 'timeout'
require 'uri'

module NevidimyTelegramTransport
  SCOPE_KEY = :nevidimy_telegram_text_transport
  TIMING_KEY = :nevidimy_telegram_send_timing
  BOT_NAMES = %w[nevidima_bot_mvp_bot nevidima_bot].freeze
  RECEIVED_AT_ATTRIBUTE = 'nevidimy_received_at_ms'.freeze
  MAX_CONNECTIONS = 4
  IDLE_SECONDS = 30
  LIFETIME_SECONDS = 300
  CHECKOUT_SECONDS = 5
  CONNECTION_ATTRIBUTES = (
    %i[address port ipaddr local_host local_port proxy_address proxy_port proxy_user proxy_pass
       open_timeout read_timeout write_timeout continue_timeout use_ssl? verify_hostname] +
    Net::HTTP::SSL_ATTRIBUTES
  ).uniq.freeze

  def self.destination?(value)
    uri = value.is_a?(URI::Generic) ? value : URI.parse(value.to_s)
    uri.scheme == 'https' && uri.host == 'api.telegram.org' && uri.port == 443 &&
      uri.userinfo.nil? && uri.query.nil? && uri.fragment.nil? &&
      %r{\A/bot[0-9]+:[A-Za-z0-9_-]+/sendMessage\z}.match?(uri.path)
  rescue URI::InvalidURIError
    false
  end

  def self.monotonic
    Process.clock_gettime(Process::CLOCK_MONOTONIC)
  end

  def self.wall_ms
    (Time.now.to_f * 1000).to_i
  end

  def self.log_send(message, timing)
    attributes = message.content_attributes
    received_at = attributes[RECEIVED_AT_ATTRIBUTE] if attributes.is_a?(Hash)
    accepted_at = timing[:accepted_at_ms]
    elapsed = accepted_at - received_at if accepted_at && received_at.is_a?(Integer) &&
                                          received_at.positive? && received_at <= accepted_at
    Rails.logger.info({
      event: 'nevidimy_telegram_send', message_id: message.id.is_a?(Integer) ? message.id : nil,
      http_elapsed_ms: timing[:http_elapsed_ms]&.round,
      send_elapsed_ms: ((monotonic - timing[:started]) * 1000).round,
      accepted_at_ms: accepted_at, source_to_accepted_ms: elapsed,
      success: timing[:success], outcome_class: timing[:outcome_class]
    }.to_json)
  rescue StandardError
    # Optional telemetry must never alter native delivery or expose error text.
    nil
  end

  class Pool
    def initialize
      @mutex = Mutex.new
      @available = ConditionVariable.new
      @connections = []
      @created_at = {}
      @idle = []
      @pid = Process.pid
    end

    def request(candidate, *arguments, &block)
      connection, key = checkout(candidate)
      reusable = false
      begin
        connection.start unless connection.started?
        response = connection.request(*arguments, &block)
        reusable = !response.connection_close?
        response
      ensure
        @mutex.synchronize do
          if reusable && connection.started? && monotonic - @created_at.fetch(connection) < LIFETIME_SECONDS
            @idle << [connection, key, monotonic]
          else
            @connections.delete(connection)
            @created_at.delete(connection)
            close(connection)
          end
          @available.broadcast
        end
      end
    end

    private

    def monotonic
      Process.clock_gettime(Process::CLOCK_MONOTONIC)
    end

    def close(connection)
      connection.finish if connection.started?
    rescue StandardError
      # A failed socket is discarded; never expose request/proxy credentials.
      nil
    end

    def checkout(candidate)
      # Only opaque digests, never URLs, bot tokens, or proxy credentials, are keys.
      settings = CONNECTION_ATTRIBUTES.map do |attribute|
        next unless candidate.respond_to?(attribute)

        value = candidate.public_send(attribute)
        [attribute, value.class.name, value.to_s]
      end.compact
      key = Digest::SHA256.hexdigest(Marshal.dump(settings))
      deadline = monotonic + CHECKOUT_SECONDS
      @mutex.synchronize do
        if @pid != Process.pid
          @connections.each { |connection| close(connection) }
          @connections.clear
          @created_at.clear
          @idle.clear
          @pid = Process.pid
        end
        loop do
          @idle.delete_if do |connection, _key, last_used|
            next false if monotonic - last_used < IDLE_SECONDS &&
                          monotonic - @created_at.fetch(connection) < LIFETIME_SECONDS

            @connections.delete(connection)
            @created_at.delete(connection)
            close(connection)
            true
          end
          index = @idle.index { |_connection, idle_key, _last_used| idle_key == key }
          return @idle.delete_at(index).first(2) if index

          # Changed proxy, CA, or timeout settings must not inherit an old session.
          if @connections.length >= MAX_CONNECTIONS && !@idle.empty?
            connection = @idle.shift.first
            @connections.delete(connection)
            @created_at.delete(connection)
            close(connection)
          end
          if @connections.length < MAX_CONNECTIONS
            @connections << candidate
            @created_at[candidate] = monotonic
            return [candidate, key]
          end

          remaining = deadline - monotonic
          raise Timeout::Error, 'Telegram connection checkout timed out' unless remaining.positive?

          @available.wait(@mutex, remaining)
        end
      end
    end
  end

  POOL = Pool.new

  class Lease
    def initialize(connection)
      @connection = connection
    end

    def request(*arguments, &block)
      POOL.request(@connection, *arguments, &block)
    end
  end

  class Adapter
    def self.call(uri, options)
      # Also checked on redirects: this specialized adapter cannot expand its origin.
      raise ArgumentError, 'Telegram transport destination is not allowed' unless NevidimyTelegramTransport.destination?(uri)

      connection = HTTParty::ConnectionAdapter.call(uri, options)
      unless connection.address == 'api.telegram.org' && connection.port == 443
        raise ArgumentError, 'Telegram transport connection is not allowed'
      end

      connection.use_ssl = true
      connection.verify_mode = OpenSSL::SSL::VERIFY_PEER
      connection.verify_hostname = true if connection.respond_to?(:verify_hostname=)
      connection.verify_callback = nil
      connection.max_retries = 0
      connection.keep_alive_timeout = IDLE_SECONDS
      Lease.new(connection)
    end
  end

  module ChannelPatch
    def send_message_on_telegram(message)
      return super unless BOT_NAMES.include?(bot_name)

      previous = Thread.current[TIMING_KEY]
      timing = { started: NevidimyTelegramTransport.monotonic, http_elapsed_ms: nil,
                 accepted_at_ms: nil, success: false, outcome_class: 'NilClass' }
      Thread.current[TIMING_KEY] = timing
      begin
        result = super
        timing[:success] = (result.is_a?(Numeric) && result.positive?) ||
                           (result.is_a?(String) && !result.empty?)
        timing[:outcome_class] = result.class.name
        timing[:accepted_at_ms] = NevidimyTelegramTransport.wall_ms if timing[:success]
        result
      rescue StandardError => error
        timing[:outcome_class] = error.class.name
        raise
      ensure
        Thread.current[TIMING_KEY] = previous
        NevidimyTelegramTransport.log_send(message, timing)
      end
    end

    private

    def message_request(*arguments, **keywords, &block)
      previous = Thread.current[SCOPE_KEY]
      Thread.current[SCOPE_KEY] = true
      started = NevidimyTelegramTransport.monotonic
      super(*arguments, **keywords, &block)
    ensure
      Thread.current[SCOPE_KEY] = previous
      timing = Thread.current[TIMING_KEY]
      if timing && started
        elapsed = (NevidimyTelegramTransport.monotonic - started) * 1000
        timing[:http_elapsed_ms] = (timing[:http_elapsed_ms] || 0) + elapsed
      end
    end
  end

  module HttpPartyPatch
    def post(url, options = {}, &block)
      if Thread.current[SCOPE_KEY] && NevidimyTelegramTransport.destination?(url)
        options = options.merge(connection_adapter: Adapter, follow_redirects: false)
      end
      super(url, options, &block)
    end
  end
end

HTTParty.singleton_class.prepend(NevidimyTelegramTransport::HttpPartyPatch)
Rails.application.reloader.to_prepare do
  Channel::Telegram.prepend(NevidimyTelegramTransport::ChannelPatch)
end

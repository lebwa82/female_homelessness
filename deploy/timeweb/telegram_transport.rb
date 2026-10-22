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
  KEEPALIVE_INTERVAL = 10
  KEEPALIVE_TIMEOUT = 2
  WARM_CONNECTIONS = 2
  REFRESH_AFTER_SECONDS = LIFETIME_SECONDS - IDLE_SECONDS
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

  def self.request_clock
    monotonic
  rescue StandardError
    nil
  end

  def self.request_delta(started, finished)
    return unless started.is_a?(Numeric) && finished.is_a?(Numeric)

    elapsed = (finished - started) * 1000
    elapsed if elapsed.finite? && elapsed >= 0
  rescue StandardError
    nil
  end

  def self.record_raw_request(timing, started, headers, callback_ms, finished)
    raw_ms = request_delta(started, finished)
    headers_ms = request_delta(started, headers)
    headers_ms = nil if raw_ms && headers_ms && headers_ms > raw_ms
    block_ms = callback_ms if headers_ms && callback_ms.is_a?(Numeric) && callback_ms.finite? && callback_ms >= 0
    tail_ms = raw_ms - headers_ms - block_ms if raw_ms && headers_ms && block_ms
    if tail_ms && tail_ms < -0.000001
      block_ms = tail_ms = nil
    elsif tail_ms
      tail_ms = [tail_ms, 0.0].max
    end
    { raw_request_ms: raw_ms, request_to_headers_ms: headers_ms,
      response_block_ms: block_ms, response_tail_ms: tail_ms }.each do |key, value|
      timing[key] = value&.round(3)
    end
  rescue StandardError
    # Optional timing, including a failed clock/recorder, must not alter delivery.
    nil
  end

  def self.verified_connection(uri, options)
    unless destination?(uri) || uri.to_s == 'https://api.telegram.org/'
      raise ArgumentError, 'Telegram transport destination is not allowed'
    end
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
    connection.set_debug_output(nil)
    connection
  end

  def self.canonical_connection
    uri = URI('https://api.telegram.org/')
    native = HTTParty.build_request(Net::HTTP::Head, uri)
    verified_connection(uri, native.options)
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
      pool_checkout_ms: timing[:pool_checkout_ms]&.round(3),
      connection_reused: timing[:connection_reused], tls_setup_ms: timing[:tls_setup_ms]&.round(3),
      raw_request_ms: timing[:raw_request_ms], request_to_headers_ms: timing[:request_to_headers_ms],
      response_block_ms: timing[:response_block_ms], response_tail_ms: timing[:response_tail_ms],
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
      @keys = {}
      @idle = []
      @pid = Process.pid
    end

    def request(candidate, *arguments, &block)
      waited = monotonic
      connection, key = checkout(candidate)
      reusable = false
      begin
        timing = Thread.current[TIMING_KEY]
        reused = connection.started?
        original_socket = connection.instance_variable_get(:@socket) if reused
        if timing
          timing[:pool_checkout_ms] = (monotonic - waited) * 1000
          timing[:connection_reused] = reused
        end
        setup = monotonic
        begin
          connection.start unless reused
        ensure
          timing[:tls_setup_ms] = reused ? 0.0 : (monotonic - setup) * 1000 if timing
        end
        raw_started = NevidimyTelegramTransport.request_clock if timing
        headers, observed_headers, callback_ms = nil, false, 0.0
        begin
          if timing && block
            response = connection.request(*arguments) do |native_response|
              entered = NevidimyTelegramTransport.request_clock
              headers = entered unless observed_headers
              observed_headers = true
              begin
                block.call(native_response)
              ensure
                elapsed = NevidimyTelegramTransport.request_delta(entered, NevidimyTelegramTransport.request_clock)
                callback_ms = callback_ms && elapsed ? callback_ms + elapsed : nil
              end
            end
          else
            # Do not introduce a response block into a blockless native request.
            response = connection.request(*arguments, &block)
          end
        ensure
          if timing
            # Ordinary HTTParty reads the body after its callback; streaming
            # callers can instead read it inside the original response block.
            NevidimyTelegramTransport.record_raw_request(timing, raw_started, headers, callback_ms,
                                                         NevidimyTelegramTransport.request_clock)
          end
        end
        reusable = !response.connection_close?
        response
      ensure
        if timing && reused
          # Net::HTTP can reconnect before transmitting when it observes an idle
          # peer's EOF. started? alone is not proof of reusing the TLS session.
          same_socket = original_socket && original_socket.equal?(connection.instance_variable_get(:@socket))
          timing[:connection_reused] = !!same_socket
          timing[:tls_setup_ms] = nil unless same_socket
        end
        release(connection, key, reusable)
      end
    end

    # Maintenance never waits for or takes a busy foreground lease. An old
    # idle session stays usable until its verified replacement is ready.
    def maintain(candidate)
      key = key_for(candidate)
      make_refresh_room(key)
      old = nil
      connection = @mutex.synchronize do
        @pid = Process.pid if @pid != Process.pid && @connections.empty?
        next nil unless @pid == Process.pid

        now = monotonic
        entry = @idle.select { |_http, idle_key, _used| idle_key == key }
                     .min_by do |http, _idle_key, used|
                       born = @created_at.fetch(http)
                       renewing = now - born >= REFRESH_AFTER_SECONDS || now - used >= IDLE_SECONDS
                       renewing ? [0, born] : [1, used]
                     end
        rotate = entry && (now - @created_at.fetch(entry[0]) >= REFRESH_AFTER_SECONDS ||
                            now - entry[2] >= IDLE_SECONDS)
        if rotate || @keys.values.count(key) < WARM_CONNECTIONS
          next nil if @connections.length >= MAX_CONNECTIONS

          old = entry[0] if rotate
          @connections << candidate
          @created_at[candidate] = now
          @keys[candidate] = key
          candidate
        elsif entry && now - entry[2] >= KEEPALIVE_INTERVAL
          @idle.delete(entry)
          entry[0]
        end
      end
      return nil unless connection

      reusable = false
      begin
        Timeout.timeout(KEEPALIVE_TIMEOUT) do
          connection.start unless connection.started?
          response = connection.request(Net::HTTP::Head.new('/'))
          # A redirect is observed, never followed; no token/body is transmitted.
          reusable = response.code.to_i.between?(200, 399) && !response.connection_close?
        end
        reusable
      rescue StandardError
        false
      ensure
        release(connection, key, reusable)
        retire_idle(old) if old && reusable
      end
    end

    def ready_count
      @mutex.synchronize do
        next 0 unless @pid == Process.pid

        @idle.count do |http, _key, used|
          http.started? && monotonic - used < IDLE_SECONDS &&
            monotonic - @created_at.fetch(http) < LIFETIME_SECONDS
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

    def key_for(candidate)
      # Only opaque digests, never URLs, bot tokens, or proxy credentials, are keys.
      settings = CONNECTION_ATTRIBUTES.map do |attribute|
        next unless candidate.respond_to?(attribute)

        value = candidate.public_send(attribute)
        [attribute, value.class.name, value.to_s]
      end.compact
      Digest::SHA256.hexdigest(Marshal.dump(settings))
    end

    def release(connection, key, reusable)
      retired = @mutex.synchronize do
        if reusable && connection.started? && monotonic - @created_at.fetch(connection) < LIFETIME_SECONDS
          @idle << [connection, key, monotonic]
          nil
        else
          @connections.delete(connection)
          @created_at.delete(connection)
          @keys.delete(connection)
          connection
        end.tap { @available.broadcast }
      end
      close(retired) if retired
    end

    def retire_idle(connection)
      retired = @mutex.synchronize do
        entry = @idle.find { |http, _key, _used| http.equal?(connection) }
        next nil unless entry

        @idle.delete(entry)
        @connections.delete(connection)
        @created_at.delete(connection)
        @keys.delete(connection)
        @available.broadcast
        connection
      end
      close(retired) if retired
    end

    def make_refresh_room(key)
      victim = @mutex.synchronize do
        next nil unless @pid == Process.pid && @connections.length >= MAX_CONNECTIONS

        entries = @idle.select { |_http, idle_key, _used| idle_key == key }
        next nil unless entries.length > 1

        entries.sort_by! { |http, _idle_key, _used| @created_at.fetch(http) }
        oldest, _idle_key, used = entries.first
        next nil unless monotonic - @created_at.fetch(oldest) >= REFRESH_AFTER_SECONDS ||
                        monotonic - used >= IDLE_SECONDS

        entries[1][0] # Retain the oldest usable socket until its replacement is ready.
      end
      retire_idle(victim) if victim
    end

    def checkout(candidate)
      key = key_for(candidate)
      deadline = monotonic + CHECKOUT_SECONDS
      @mutex.synchronize do
        if @pid != Process.pid
          @connections.each { |connection| close(connection) }
          @connections.clear
          @created_at.clear
          @keys.clear
          @idle.clear
          @pid = Process.pid
        end
        loop do
          @idle.delete_if do |connection, _key, last_used|
            next false if monotonic - last_used < IDLE_SECONDS &&
                          monotonic - @created_at.fetch(connection) < LIFETIME_SECONDS

            @connections.delete(connection)
            @created_at.delete(connection)
            @keys.delete(connection)
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
            @keys.delete(connection)
            close(connection)
          end
          if @connections.length < MAX_CONNECTIONS
            @connections << candidate
            @created_at[candidate] = monotonic
            @keys[candidate] = key
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

  class Keepalive
    def initialize(pool = POOL)
      @pool = pool
      @mutex = Mutex.new
      @condition = ConditionVariable.new
      @running = false
    end

    def start
      @mutex.synchronize do
        return if @pid == Process.pid && @running

        @pid = Process.pid
        @stopped = false
        @running = true
      end
      # Sidekiq fires startup after application warmup and before job fetching.
      tick
      log('startup')
      @mutex.synchronize do
        if @stopped
          @running = false
          return
        end
        @thread = Thread.new do
          begin
            loop do
              stopped = @mutex.synchronize do
                @condition.wait(@mutex, KEEPALIVE_INTERVAL) unless @stopped
                @stopped || @pid != Process.pid
              end
              break if stopped

              tick
            end
          ensure
            @mutex.synchronize { @running = false if @pid == Process.pid }
          end
        end
        @thread.name = 'telegram-keepalive' if @thread.respond_to?(:name=)
      end
    rescue StandardError
      @mutex.synchronize { @running = false }
      log('unavailable')
      nil
    end

    def stop(wait: false)
      thread = @mutex.synchronize do
        return unless @pid == Process.pid

        @stopped = true
        @condition.broadcast
        @thread
      end
      thread.join(KEEPALIVE_TIMEOUT * WARM_CONNECTIONS + 1) if wait && thread && thread != Thread.current
    end

    private

    def tick
      results = WARM_CONNECTIONS.times.map { @pool.maintain(NevidimyTelegramTransport.canonical_connection) }
      if results.include?(false)
        log('unavailable') unless @failed
        @failed = true
      elsif results.include?(true)
        log('recovered') if @failed
        @failed = false
      end
    rescue StandardError
      log('unavailable') unless @failed
      @failed = true
    end

    def log(phase)
      Rails.logger.info({ event: 'nevidimy_telegram_keepalive', phase: phase,
                          ready: @pool.ready_count, pid: Process.pid }.to_json)
    rescue StandardError
      nil
    end
  end

  KEEPALIVE = Keepalive.new

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

      Lease.new(NevidimyTelegramTransport.verified_connection(uri, options))
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

if defined?(Sidekiq)
  Sidekiq.configure_server do |config|
    config.on(:startup) { NevidimyTelegramTransport::KEEPALIVE.start }
    config.on(:quiet) { NevidimyTelegramTransport::KEEPALIVE.stop }
    config.on(:shutdown) { NevidimyTelegramTransport::KEEPALIVE.stop(wait: true) }
  end
end

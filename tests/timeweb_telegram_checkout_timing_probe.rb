# Neutral checkout attribution contracts: no sockets, Rails app or credentials.
require 'json'
require 'ostruct'

module Rails
  class << self
    attr_accessor :logger

    def application
      OpenStruct.new(reloader: Object.new.tap { |reloader| def reloader.to_prepare; end })
    end
  end
end

module HTTParty; end
require_relative '../deploy/timeweb/telegram_transport'

def verify(value)
  raise 'neutral checkout timing contract failed' unless value
end

Clock = Struct.new(:wall, :cpu, :broken) do
  def advance(wall_ms, cpu_ms = 0)
    self.wall += wall_ms / 1000.0
    self.cpu += cpu_ms / 1000.0 if cpu
  end
end

class << NevidimyTelegramTransport
  attr_accessor :probe_clock

  def monotonic
    raise IOError, 'neutral clock failure' if probe_clock.broken

    probe_clock.wall
  end
end

class NeutralConnection
  Response = Struct.new(:unused) do
    def connection_close?
      false
    end
  end

  attr_accessor :failure
  attr_reader :calls, :finishes, :arguments, :had_block, :block_result, :response

  def initialize
    @socket, @response = Object.new, Response.new
    @calls, @finishes = 0, 0
  end

  def started?
    true
  end

  def request(*arguments, &block)
    @calls += 1
    @arguments, @had_block = arguments, !block.nil?
    raise failure if failure

    @block_result = block.call(response) if block
    response
  end

  def finish
    @finishes += 1
    NevidimyTelegramTransport.probe_clock.advance(7, 2)
  end
end

class NeutralMutex
  def initialize(clock)
    @clock = clock
  end

  def synchronize
    @clock.advance(2)
    yield
  end
end

class NeutralAvailable
  attr_reader :waits

  def initialize(clock, &release)
    @clock, @release, @waits = clock, release, 0
  end

  def wait(_mutex, _remaining)
    @waits += 1
    @clock.advance(@release ? 5 : 6000)
    @release&.call
  end

  def broadcast; end
end

class NeutralPool < NevidimyTelegramTransport::Pool
  attr_accessor :key_failure
  attr_reader :released

  def initialize(clock)
    super()
    @clock, @mutex = clock, NeutralMutex.new(clock)
  end

  def seed(connection, key: :neutral, age: 0)
    @connections << connection
    @created_at[connection] = @clock.wall
    @keys[connection] = key
    @idle << [connection, key, @clock.wall - age]
  end

  def saturate(connection, release: true)
    @connections = Array.new(4) { NeutralConnection.new }
    @connections[0] = connection
    @connections.each { |http| @created_at[http] = @clock.wall; @keys[http] = :neutral }
    @available = if release
                   NeutralAvailable.new(@clock) { @idle << [connection, :neutral, @clock.wall] }
                 else
                   NeutralAvailable.new(@clock)
                 end
  end

  private

  def monotonic
    @clock.wall
  end

  def key_for(_candidate)
    @clock.advance(3, 1)
    raise key_failure if key_failure

    :neutral
  end

  def release(connection, key, reusable)
    @released = [connection, key, reusable]
  end
end

FIELDS = %i[pool_key_ms pool_mutex_wait_ms pool_lease_wait_ms pool_idle_closed pool_checkout_cpu_ms].freeze
def context
  clock = Clock.new(10.0, 1.0, false)
  NevidimyTelegramTransport.probe_clock = clock
  timing = {}
  Thread.current[NevidimyTelegramTransport::TIMING_KEY] = timing
  [clock, timing, NeutralPool.new(clock), NeutralConnection.new]
end

original_cpu_clock = Process.method(:clock_gettime)
Process.define_singleton_method(:clock_gettime) do |id, *arguments|
  if id == Process::CLOCK_THREAD_CPUTIME_ID
    raise NotImplementedError unless NevidimyTelegramTransport.probe_clock.cpu

    NevidimyTelegramTransport.probe_clock.cpu
  else
    original_cpu_clock.call(id, *arguments)
  end
end

report = {}
begin
  clock, timing, pool, connection = context
  pool.seed(connection)
  request, marker = Object.new, Object.new
  verify(pool.request(NeutralConnection.new, request) { |response| verify(response.equal?(connection.response)); marker }.equal?(connection.response))
  verify(connection.calls == 1 && connection.arguments == [request] && connection.block_result.equal?(marker))
  verify(pool.released == [connection, :neutral, true])
  verify(timing.values_at(*FIELDS) == [3.0, 2.0, 0.0, 0, 1.0] && timing[:pool_checkout_ms].round(3) == 5.0)
  report['reuse_key_mutex_and_cpu_attribution'] = true

  clock, timing, pool, connection = context
  pool.saturate(connection)
  verify(pool.request(NeutralConnection.new, request).equal?(connection.response) && !connection.had_block)
  verify(timing.values_at(*FIELDS) == [3.0, 2.0, 5.0, 0, 1.0] && timing[:pool_checkout_ms].round(3) == 10.0)
  report['lease_wait_is_distinct_from_mutex_wait'] = true

  clock, timing, pool, connection = context
  old = NeutralConnection.new
  pool.seed(old, age: 31)
  verify(pool.request(connection, request).equal?(connection.response))
  verify(old.finishes == 1 && timing[:pool_idle_closed] == 1 && timing[:pool_checkout_cpu_ms] == 3.0)
  verify(timing[:pool_checkout_ms].round(3) == 12.0)
  report['expired_idle_close_count_and_cpu_included'] = true

  clock, timing, pool, connection = context
  4.times { pool.seed(NeutralConnection.new, key: :different) }
  verify(pool.request(connection, request).equal?(connection.response) && timing[:pool_idle_closed] == 1)
  report['different_identity_idle_eviction_counted'] = true

  clock, timing, pool, connection = context
  failure = ArgumentError.new('neutral key failure')
  pool.key_failure = failure
  begin
    pool.request(connection, request)
    raise 'native key failure swallowed'
  rescue ArgumentError => error
    verify(error.equal?(failure) && connection.calls.zero? && pool.released.nil?)
  end
  verify(timing[:pool_key_ms] == 3.0)
  report['key_exception_object_and_no_delivery_preserved'] = true

  clock, timing, pool, connection = context
  pool.saturate(connection, release: false)
  begin
    pool.request(NeutralConnection.new, request)
    raise 'native timeout swallowed'
  rescue Timeout::Error
    verify(connection.calls.zero? && pool.released.nil? && timing[:pool_lease_wait_ms] == 6000.0)
  end
  report['native_checkout_timeout_without_retry_preserved'] = true

  clock, timing, pool, connection = context
  pool.seed(connection)
  failure = IOError.new('neutral native failure')
  connection.failure = failure
  begin
    pool.request(connection, request)
    raise 'native request failure swallowed'
  rescue IOError => error
    verify(error.equal?(failure) && connection.calls == 1 && pool.released == [connection, :neutral, false])
  end
  report['native_request_exception_and_release_preserved'] = true

  clock, timing, pool, connection = context
  pool.seed(connection)
  clock.broken = true
  verify(pool.request(connection, request).equal?(connection.response))
  verify(timing[:pool_key_ms].nil? && timing[:pool_mutex_wait_ms].nil? &&
         timing[:pool_lease_wait_ms] == 0.0 && timing[:pool_idle_closed] == 0)
  report['optional_wall_clock_failure_does_not_change_delivery'] = true

  clock, timing, pool, connection = context
  pool.seed(connection)
  clock.cpu = nil
  verify(pool.request(connection, request).equal?(connection.response) && timing[:pool_checkout_cpu_ms].nil?)
  verify(timing[:pool_key_ms] == 3.0 && timing[:pool_mutex_wait_ms] == 2.0)
  report['unavailable_cpu_clock_keeps_wall_attribution'] = true

  clock, timing, pool, connection = context
  pool.seed(connection)
  failure = ArgumentError.new('neutral callback failure')
  begin
    pool.request(connection, request) { raise failure }
    raise 'native callback failure swallowed'
  rescue ArgumentError => error
    verify(error.equal?(failure) && connection.calls == 1 && pool.released == [connection, :neutral, false])
  end
  report['callback_exception_object_and_no_retry_preserved'] = true

  clock, timing, pool, connection = context
  pool.seed(connection)
  timing.freeze
  verify(pool.send(:checkout, connection) == [connection, :neutral])
  report['frozen_optional_recorder_keeps_selected_lease'] = true

  clock, timing, pool, connection = context
  pool.seed(connection)
  Thread.current[NevidimyTelegramTransport::TIMING_KEY] = nil
  verify(pool.request(connection, request).equal?(connection.response) && timing.empty?)
  report['inactive_telemetry_keeps_native_result'] = true

  clock, timing, pool, connection = context
  pool.seed(connection)
  captured = []
  Rails.logger = Object.new.tap { |logger| logger.define_singleton_method(:info) { |line| captured << JSON.parse(line) } }
  pool.request(connection, request)
  timing.merge!(started: 10.0, private_probe_marker: 'not-for-output')
  NevidimyTelegramTransport.log_send(OpenStruct.new(id: 1, content_attributes: {}), timing)
  verify(FIELDS.all? { |key| captured.one? && captured[0].key?(key.to_s) })
  verify(!captured[0].key?('private_probe_marker') && !captured[0].key?('started'))
  report['only_fixed_content_free_keys_are_logged'] = true
ensure
  Process.define_singleton_method(:clock_gettime, original_cpu_clock)
  Thread.current[NevidimyTelegramTransport::TIMING_KEY] = nil
end
puts JSON.generate(report)

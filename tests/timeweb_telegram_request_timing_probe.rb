# Deterministic request-boundary probe. No sockets, credentials, Rails or API calls.
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
  raise 'neutral request timing contract failed' unless value
end

class << NevidimyTelegramTransport
  attr_accessor :probe_clock, :probe_clock_failure

  def monotonic
    raise 'neutral telemetry clock failure' if probe_clock_failure

    probe_clock
  end
end

class TimingConnection
  Response = Struct.new(:unused) do
    def connection_close?
      false
    end
  end

  attr_reader :response, :block_result, :calls, :had_block, :arguments, :received_block
  attr_accessor :failure

  def initialize
    @socket, @response, @calls = Object.new, Response.new, 0
  end

  def started?
    true
  end

  def request(*arguments, &block)
    @arguments, @had_block, @received_block = arguments, !block.nil?, block
    @calls += 1
    NevidimyTelegramTransport.probe_clock += 0.003
    raise failure if failure

    @block_result = block.call(response) if block
    NevidimyTelegramTransport.probe_clock += 0.005
    response
  end
end

class TimingPool < NevidimyTelegramTransport::Pool
  attr_reader :released

  private

  def checkout(candidate)
    [candidate, :neutral]
  end

  def release(connection, key, reusable)
    @released = [connection, key, reusable]
  end
end

KEYS = %i[raw_request_ms request_to_headers_ms response_block_ms response_tail_ms].freeze
def context
  NevidimyTelegramTransport.probe_clock = 10.0
  NevidimyTelegramTransport.probe_clock_failure = false
  Thread.current[NevidimyTelegramTransport::TIMING_KEY] = {}
end

report = {}
timing = context
connection, pool, request, block_result = TimingConnection.new, TimingPool.new, Object.new, Object.new
result = pool.request(connection, request) do |response|
  verify(response.equal?(connection.response))
  NevidimyTelegramTransport.probe_clock += 0.004
  block_result
end
verify(result.equal?(connection.response) && connection.block_result.equal?(block_result) &&
       connection.calls == 1 && connection.arguments == [request] && pool.released == [connection, :neutral, true])
report['native_results_arguments_and_reuse_unchanged'] = true
verify(KEYS.map { |key| timing[key] } == [12.0, 3.0, 4.0, 5.0])
verify((timing[:request_to_headers_ms] + timing[:response_block_ms] + timing[:response_tail_ms] - timing[:raw_request_ms]).abs < 0.001)
report['three_marks_and_callback_are_nonoverlapping'] = true

timing = context
connection, pool = TimingConnection.new, TimingPool.new
verify(pool.request(connection, request).equal?(connection.response) && !connection.had_block &&
       timing[:raw_request_ms] == 8.0 && KEYS.drop(1).all? { |key| timing[key].nil? })
report['blockless_native_request_has_no_added_block'] = true

context
Thread.current[NevidimyTelegramTransport::TIMING_KEY] = nil
connection, pool = TimingConnection.new, TimingPool.new
original = proc { :neutral }
verify(pool.request(connection, request, &original).equal?(connection.response) && connection.received_block.equal?(original))
report['inactive_telemetry_passes_original_block_unchanged'] = true

timing = context
connection, pool, failure = TimingConnection.new, TimingPool.new, ArgumentError.new('neutral callback failure')
begin
  pool.request(connection, request) { raise failure }
  raise 'native callback failure swallowed'
rescue ArgumentError => error
  verify(error.equal?(failure) && connection.calls == 1 && pool.released == [connection, :neutral, false])
end
verify(timing[:raw_request_ms] == 3.0 && timing[:response_block_ms] == 0.0)
report['callback_exception_object_and_no_retry_preserved'] = true

context
connection, pool = TimingConnection.new, TimingPool.new
verify(pool.request(connection, request) { break :neutral_break } == :neutral_break)
verify(pool.released == [connection, :neutral, false])
report['native_nonlocal_block_exit_preserved'] = true

timing = context
connection, pool, failure = TimingConnection.new, TimingPool.new, IOError.new('neutral request failure')
connection.failure = failure
begin
  pool.request(connection, request) { raise 'unexpected callback' }
  raise 'native request failure swallowed'
rescue IOError => error
  verify(error.equal?(failure) && connection.calls == 1 && pool.released == [connection, :neutral, false])
end
verify(timing[:raw_request_ms] == 3.0 && timing[:request_to_headers_ms].nil?)
report['request_failure_before_headers_preserved'] = true

timing = context
NevidimyTelegramTransport.probe_clock_failure = true
connection, pool = TimingConnection.new, TimingPool.new
verify(pool.request(connection, request) { :neutral }.equal?(connection.response))
verify(KEYS.all? { |key| timing[key].nil? } && pool.released == [connection, :neutral, true])
report['optional_clock_failure_does_not_change_delivery'] = true

connection, pool, failure = TimingConnection.new, TimingPool.new, ArgumentError.new('neutral callback failure')
begin
  pool.request(connection, request) { raise failure }
  raise 'native callback failure swallowed'
rescue ArgumentError => error
  verify(error.equal?(failure) && pool.released == [connection, :neutral, false])
end
report['clock_failure_does_not_replace_native_exception'] = true

class BrokenRecorder < Hash
  def []=(key, value)
    raise 'neutral telemetry recorder failure' if KEYS.include?(key)

    super
  end
end
context
Thread.current[NevidimyTelegramTransport::TIMING_KEY] = BrokenRecorder.new
connection, pool = TimingConnection.new, TimingPool.new
verify(pool.request(connection, request) { :neutral }.equal?(connection.response) &&
       pool.released == [connection, :neutral, true])
report['optional_recorder_failure_does_not_change_delivery'] = true

NevidimyTelegramTransport.record_raw_request({}.freeze, 1.0, 1.001, 1.0, 1.002)
invalid = {}
NevidimyTelegramTransport.record_raw_request(invalid, 2.0, 1.0, 0.0, 3.0)
verify(invalid[:request_to_headers_ms].nil? && invalid[:response_block_ms].nil? && invalid[:response_tail_ms].nil?)
report['frozen_or_backward_telemetry_does_not_escape'] = true

context
logs = []
Rails.logger = Object.new
Rails.logger.define_singleton_method(:info) { |value| logs << JSON.parse(value) }
timing = { started: 10.0, success: true, outcome_class: 'Integer', raw_request_ms: 12.0,
           request_to_headers_ms: 3.0, response_block_ms: 4.0, response_tail_ms: 5.0 }
NevidimyTelegramTransport.log_send(OpenStruct.new(id: 1, content_attributes: {}), timing)
keys = %w[event message_id http_elapsed_ms pool_checkout_ms connection_reused tls_setup_ms
          send_elapsed_ms accepted_at_ms source_to_accepted_ms success outcome_class] + KEYS.map(&:to_s)
verify(logs.length == 1 && logs.first.keys.sort == keys.sort && logs.first['raw_request_ms'] == 12.0)
report['fixed_content_free_fields_only'] = true

Thread.current[NevidimyTelegramTransport::TIMING_KEY] = nil
puts JSON.generate(report)

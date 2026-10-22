# Pure Ruby collaborators only; no Rails process, records, dialogue or network.
require 'json'
require 'ostruct'

def verify(value, label)
  raise label unless value
end

module ActiveSupport
  module Notifications
    class << self
      attr_reader :subscriptions

      def monotonic_subscribe(name, callback)
        @subscriptions ||= []
        @subscriptions << [name, callback]
        callback
      end

      def emit(payload, duration = 0.001)
        (@subscriptions || []).each do |name, callback|
          callback.call(name, 1.0, 1.0 + duration, 'unused', payload)
        end
      end
    end
  end
end

class ProbeLogger
  attr_reader :events
  attr_accessor :failure

  def initialize
    @events = []
    @mutex = Mutex.new
  end

  def info(value)
    raise failure if failure

    @mutex.synchronize { @events << JSON.parse(value) }
  end
end

class ProbeReloader
  def initialize
    @prepares = []
  end

  def to_prepare(&block)
    @prepares << block
  end

  def prepare
    @prepares.each(&:call)
  end
end

module Rails
  def self.application
    @application ||= OpenStruct.new(reloader: ProbeReloader.new)
  end

  def self.logger
    @logger ||= ProbeLogger.new
  end
end

# The subscriber may inspect only these two classification fields. Reading SQL,
# binds, exception data or other notification fields fails the probe.
class ProbeSqlPayload
  def initialize(name = 'PRIVATE_SENTINEL', cached = false)
    @name, @cached = name, cached
  end

  def [](key)
    return @name if key == :name
    return @cached if key == :cached

    raise 'notification fields must not be read'
  end
end

class ProbeFailure < StandardError; end

class Message
  CALLBACK_ORDER = %i[
    reopen_conversation mark_pending_conversation_as_open_for_human_response
    set_conversation_activity dispatch_create_events send_reply
    execute_message_template_hooks update_contact_activity
  ].freeze
  attr_reader :id, :order, :failure, :fail_at

  def initialize(id, failure: nil, fail_at: nil, outgoing: true, private_message: false)
    @id, @failure, @fail_at, @outgoing = id, failure, fail_at, outgoing
    @private_message = private_message
    @order = []
  end

  def outgoing?
    @outgoing
  end

  def incoming?
    !@outgoing
  end

  def private?
    @private_message
  end

  def run_callbacks
    CALLBACK_ORDER.each { |name| send(name) }
  end

  CALLBACK_ORDER.each do |name|
    define_method(name) do
      order << name
      ActiveSupport::Notifications.emit(ProbeSqlPayload.new)
      raise failure if name == fail_at

      :native_callback_return
    end
    private name
  end
end

class ProbeListener
  attr_accessor :failure

  def message_created(event)
    raise failure if failure

    event
  end

  def conversation_updated(event)
    raise failure if failure

    event
  end
end

class ActionCableListener < ProbeListener; end
class AgentBotListener < ProbeListener; end

module Webhooks
  class TelegramEventsJob
    attr_reader :calls, :received
    attr_accessor :failure

    def perform(params = {})
      @calls = (@calls || 0) + 1
      @received = params
      row = Message.new(456, outgoing: false, failure: failure, fail_at: failure && :dispatch_create_events)
      row.run_callbacks
      :native_input_return
    end
  end
end

module Messages
  class MessageBuilder
    def initialize(message, cold: false, failure: nil)
      @message, @cold, @failure = message, cold, failure
    end

    def perform
      ActiveSupport::Notifications.emit(ProbeSqlPayload.new('SCHEMA')) if @cold
      ActiveSupport::Notifications.emit(ProbeSqlPayload.new)
      ActiveSupport::Notifications.emit(ProbeSqlPayload.new('PRIVATE_SENTINEL', true))
      raise @failure if @failure

      @message
    end
  end
end

module Api
  module V1
    module Accounts
      module Conversations
        class MessagesController
          attr_reader :params, :response, :native_message, :calls

          def initialize(atomic: true, cold: false, id: 123, failure: nil, fail_at: nil,
                         builder_failure: nil, gate: nil, outgoing: true, private_message: false)
            @params = atomic ? { nevidimy_atomic_turn: 'PRIVATE_SENTINEL' } : {}
            @response = OpenStruct.new(status: 201)
            @native_message = Message.new(id, failure: failure, fail_at: fail_at, outgoing: outgoing,
                                         private_message: private_message)
            @cold, @builder_failure, @gate, @calls = cold, builder_failure, gate, 0
          end

          def create
            @calls += 1
            @gate.call if @gate
            ActiveSupport::Notifications.emit(ProbeSqlPayload.new)
            Messages::MessageBuilder.new(native_message, cold: @cold, failure: @builder_failure).perform
            native_message.run_callbacks
            :native_controller_return
          end
        end
      end
    end
  end
end

initializer = File.expand_path('../deploy/timeweb/turn_timing.rb', __dir__)
load initializer
Rails.application.reloader.prepare
controller_class = Api::V1::Accounts::Conversations::MessagesController
report = {}

cold = controller_class.new(cold: true)
verify(cold.create == :native_controller_return, 'native return changed')
first = Rails.logger.events.last
verify(first['sql'].transform_values { |value| value['count'] } ==
       { 'schema' => 1, 'cached' => 1, 'uncached' => 9 }, 'SQL classification')
verify(first['sql'].values.all? { |value| (value['elapsed_ms'] - value['count']).abs < 0.001 }, 'SQL duration sum')
verify(first['outgoing_message_id'] == 123 && first['http_status'] == 201, 'allowed response metadata')
verify(first['spans'].keys.sort == (['message_builder'] + Message::CALLBACK_ORDER.map(&:to_s) +
       %w[ui_message_created ui_conversation_updated bot_message_created bot_conversation_updated]).sort,
       'span allowlist')
verify((['message_builder'] + Message::CALLBACK_ORDER.map(&:to_s)).all? { |name|
  first['spans'][name]['count'] == 1 && first['spans'][name]['elapsed_ms'] >= 0
}, 'native spans')
verify(NevidimyTurnTiming::LISTENER_SPANS.all? { |name| first['spans'][name.to_s]['count'].zero? }, 'unused listeners measured')
verify(cold.native_message.order == Message::CALLBACK_ORDER, 'native callback ordering changed')
verify(!first.key?('process_gc_ms'), 'missing GC API was not optional') unless GC.respond_to?(:total_time)
report[:cold_native_order_and_sql] = true

warm = controller_class.new(id: 124)
verify(warm.create == :native_controller_return, 'warm native return')
verify(Rails.logger.events.last['sql']['schema']['count'].zero?, 'cold counters leaked into warm request')
verify(Rails.logger.events.last['sql']['uncached']['count'] == 9, 'warm SQL counts')
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'request scope leaked')
report[:warm_scope_isolation] = true

before = Rails.logger.events.length
verify(controller_class.new(atomic: false).create == :native_controller_return, 'non-atomic native return')
ActiveSupport::Notifications.emit(ProbeSqlPayload.new('SCHEMA'))
verify(Rails.logger.events.length == before, 'non-atomic request logged')
report[:atomic_requests_only] = true

failure = ProbeFailure.new('PRIVATE_SENTINEL')
failed = controller_class.new(failure: failure, fail_at: :dispatch_create_events)
begin
  failed.create
  raise 'native exception missing'
rescue ProbeFailure => caught
  verify(caught.equal?(failure), 'native exception identity changed')
end
verify(failed.native_message.order == Message::CALLBACK_ORDER.first(4), 'callbacks continued after exception')
verify(!Rails.logger.events.last.key?('http_status'), 'exception logged a pre-middleware status')
verify(Rails.logger.events.last['spans']['dispatch_create_events']['count'] == 1, 'raising callback not measured')
verify(Rails.logger.events.last['spans']['send_reply']['count'].zero?, 'skipped callback measured')
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'exception scope leaked')
report[:callback_exception_preserved] = true

builder_failed = controller_class.new(builder_failure: failure)
begin
  builder_failed.create
rescue ProbeFailure => caught
  verify(caught.equal?(failure), 'builder exception identity changed')
end
verify(builder_failed.calls == 1 && builder_failed.native_message.order.empty?, 'builder failure retried or reordered')
verify(Rails.logger.events.last['spans']['message_builder']['count'] == 1, 'raising builder not measured')
report[:builder_exception_preserved] = true

Rails.logger.failure = RuntimeError.new('PRIVATE_SENTINEL')
verify(controller_class.new.create == :native_controller_return, 'logger changed native return')
begin
  controller_class.new(failure: failure, fail_at: :send_reply).create
rescue ProbeFailure => caught
  verify(caught.equal?(failure), 'logger replaced native exception')
ensure
  Rails.logger.failure = nil
end
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'logger failure scope leaked')
report[:logger_failure_is_optional] = true

load initializer
2.times { Rails.application.reloader.prepare }
verify(ActiveSupport::Notifications.subscriptions.length == 1, 'reload duplicated SQL subscriber')
before = Rails.logger.events.length
controller_class.new.create
verify(Rails.logger.events.length == before + 1, 'reload duplicated controller wrapper')
verify((['message_builder'] + Message::CALLBACK_ORDER.map(&:to_s)).all? { |name|
  Rails.logger.events.last['spans'][name]['count'] == 1
}, 'reload duplicated native wrapper')
verify(Message.private_instance_methods.include?(:send_reply), 'private callback visibility changed')
report[:reload_idempotency] = true

class << NevidimyTurnTiming
  alias_method :probe_real_monotonic, :monotonic
  alias_method :probe_real_wall_ms, :wall_ms

  def monotonic
    raise 'PRIVATE_SENTINEL' if Thread.current[:probe_clock_failure]
    return Thread.current[:probe_monotonic_values].shift if Thread.current[:probe_monotonic_values]

    probe_real_monotonic
  end

  def wall_ms
    raise Thread.current[:probe_wall_failure] if Thread.current[:probe_wall_failure]
    return Thread.current[:probe_wall_values].shift if Thread.current[:probe_wall_values]

    probe_real_wall_ms
  end
end
Thread.current[:probe_clock_failure] = true
before = Rails.logger.events.length
clock_failed = controller_class.new
verify(clock_failed.create == :native_controller_return && clock_failed.calls == 1, 'clock failure changed native work')
verify(Rails.logger.events.length == before, 'failed timing initialization logged')
Thread.current[:probe_clock_failure] = nil
active_clock_failed = controller_class.new(gate: -> { Thread.current[:probe_clock_failure] = true })
verify(active_clock_failed.create == :native_controller_return && active_clock_failed.calls == 1,
       'active clock failure changed native work')
verify(active_clock_failed.native_message.order == Message::CALLBACK_ORDER, 'active clock failure changed callback order')
Thread.current[:probe_clock_failure] = nil
begin
  controller_class.new(failure: failure, fail_at: :send_reply,
                       gate: -> { Thread.current[:probe_clock_failure] = true }).create
rescue ProbeFailure => caught
  verify(caught.equal?(failure), 'clock failure replaced native exception')
ensure
  Thread.current[:probe_clock_failure] = nil
end
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'active clock failure scope leaked')
[RuntimeError.new('PRIVATE_SENTINEL'), NotImplementedError.new('PRIVATE_SENTINEL')].each do |wall_failure|
  before = Rails.logger.events.length
  Thread.current[:probe_wall_failure] = wall_failure
  wall_failed = controller_class.new
  verify(wall_failed.create == :native_controller_return && wall_failed.calls == 1, 'wall clock failure changed native return')
  Thread.current[:probe_wall_failure] = nil
  begin
    controller_class.new(failure: failure, fail_at: :send_reply,
                         gate: -> { Thread.current[:probe_wall_failure] = wall_failure }).create
  rescue ProbeFailure => caught
    verify(caught.equal?(failure), 'wall clock failure replaced native exception')
  ensure
    Thread.current[:probe_wall_failure] = nil
  end
  verify(Rails.logger.events.length == before && Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?,
         'wall clock failure logged or leaked scope')
end
report[:clock_failure_is_optional] = true

previous = Object.new
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = previous
controller_class.new.create
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].equal?(previous), 'previous scope not restored')
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = nil
report[:previous_scope_restored] = true

ready, release = Queue.new, Queue.new
worker = Thread.new do
  controller_class.new(id: 125, gate: -> { ready << true; release.pop }).create
end
ready.pop
ActiveSupport::Notifications.emit(ProbeSqlPayload.new('SCHEMA'))
controller_class.new(id: 126).create
release << true
verify(worker.join(2), 'worker did not finish')
events = Rails.logger.events.select { |event| [125, 126].include?(event['outgoing_message_id']) }
verify(events.length == 2 && events.all? { |event| event['sql']['uncached']['count'] == 9 &&
                                               event['sql']['schema']['count'].zero? }, 'thread counters mixed')
report[:thread_scope_isolation] = true

module ProbeResourceCounters
  def self.read(name)
    samples = Thread.current[:probe_resource_counters].fetch(name)
    value = samples.length > 1 ? samples.shift : samples.first
    raise value if value.is_a?(Exception)

    value
  end
end

class << Process
  alias_method :probe_native_clock_gettime, :clock_gettime

  def clock_gettime(clock, *units)
    if clock == Process::CLOCK_THREAD_CPUTIME_ID && Thread.current[:probe_cpu_reads]
      Thread.current[:probe_cpu_reads] += 1
    end
    if clock == Process::CLOCK_THREAD_CPUTIME_ID && Thread.current[:probe_resource_counters]
      return ProbeResourceCounters.read(:cpu)
    end

    probe_native_clock_gettime(clock, *units)
  end
end

class << GC
  alias_method :probe_native_stat, :stat
  alias_method :probe_native_total_time, :total_time if method_defined?(:total_time)

  def stat(*arguments)
    if arguments == [:total_allocated_objects] && Thread.current[:probe_resource_counters]
      return ProbeResourceCounters.read(:allocations)
    end

    probe_native_stat(*arguments)
  end

  def total_time
    return ProbeResourceCounters.read(:gc) if Thread.current[:probe_resource_counters]
    return probe_native_total_time if respond_to?(:probe_native_total_time)

    raise NotImplementedError
  end
end

def resource_samples
  { cpu: [1_000_000_000, 1_025_000_000], gc: [2_000_000_000, 2_002_000_000], allocations: [1000, 1042] }
end

resource_fields = %w[thread_cpu_ms process_gc_ms process_allocated_objects]
Thread.current[:probe_resource_counters] = resource_samples
verify(controller_class.new.create == :native_controller_return, 'resource timing changed native return')
metrics = Rails.logger.events.last
verify(resource_fields.map { |key| metrics[key] } == [25.0, 2.0, 42], 'resource deltas or units changed')
verify(resource_fields.all? { |key| metrics[key].finite? && metrics[key] >= 0 }, 'invalid resource delta')
Thread.current[:probe_resource_counters] = resource_samples.transform_values { |values| [values.first, values.first] }
controller_class.new.create
verify(resource_fields.map { |key| Rails.logger.events.last[key] } == [0.0, 0.0, 0], 'zero resource deltas were omitted')
report[:resource_deltas_are_finite_and_nonnegative] = true

{ cpu: 'thread_cpu_ms', gc: 'process_gc_ms', allocations: 'process_allocated_objects' }.each do |counter, field|
  [NotImplementedError.new('PRIVATE_SENTINEL'), NoMethodError.new('PRIVATE_SENTINEL'), RuntimeError.new('PRIVATE_SENTINEL'),
   nil, 'PRIVATE_SENTINEL', Float::INFINITY, Float::NAN, -1].each do |invalid|
    [0, 1].each do |position|
      samples = resource_samples
      samples[counter][position] = invalid
      Thread.current[:probe_resource_counters] = samples
      before = Rails.logger.events.length
      verify(controller_class.new.create == :native_controller_return, 'optional counter changed native return')
      metrics = Rails.logger.events.last
      verify(Rails.logger.events.length == before + 1 && !metrics.key?(field) &&
             (resource_fields - [field]).all? { |key| metrics.key?(key) },
             'optional counter failure suppressed native timing or other counters')
    end
  end
  samples = resource_samples
  samples[counter] = [100, 99]
  Thread.current[:probe_resource_counters] = samples
  controller_class.new.create
  verify(!Rails.logger.events.last.key?(field), 'decreasing counter was reported')
end
Thread.current[:probe_resource_counters] = resource_samples.transform_values { |_value| [NotImplementedError.new, NotImplementedError.new] }
begin
  controller_class.new(failure: failure, fail_at: :send_reply).create
  raise 'native failure swallowed by resource timing'
rescue ProbeFailure => caught
  verify(caught.equal?(failure), 'resource failure replaced native exception')
end
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'resource failure leaked request scope')
report[:resource_apis_and_failures_are_optional] = true

Thread.current[:probe_resource_counters] = {
  cpu: [100_000_000, 120_000_000] + Array.new(16, 120_000_000) + [150_000_000] +
       Array.new(16, 150_000_000) + [200_000_000],
  gc: [1_000_000_000, 1_100_000_000, 1_120_000_000, 1_200_000_000],
  allocations: [1000, 1100, 1120, 1200]
}
controller_class.new(id: 127, gate: -> { controller_class.new(id: 128).create }).create
inner, outer = Rails.logger.events.last(2)
verify(inner['outgoing_message_id'] == 128 && outer['outgoing_message_id'] == 127, 'nested resource event ordering')
verify(resource_fields.map { |key| inner[key] } == [30.0, 20.0, 20] &&
       resource_fields.map { |key| outer[key] } == [100.0, 200.0, 200], 'nested resource baselines mixed')
Thread.current[:probe_resource_counters] = resource_samples
controller_class.new.create
verify(resource_fields.map { |key| Rails.logger.events.last[key] } == [25.0, 2.0, 42], 'warm resource baselines leaked')
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'nested resource scope leaked')
Thread.current[:probe_resource_counters] = nil
report[:resource_nested_and_warm_scope_isolation] = true

controller_class.new(id: 'PRIVATE_SENTINEL').create
verify(!Rails.logger.events.last.key?('outgoing_message_id'), 'noninteger message id logged')
controller_class.new(outgoing: false).create
verify(!Rails.logger.events.last.key?('outgoing_message_id'), 'nonoutgoing message id logged')
invalid_status = controller_class.new
invalid_status.response.status = 'PRIVATE_SENTINEL'
invalid_status.create
verify(!Rails.logger.events.last.key?('http_status'), 'invalid status logged')
allowed = %w[event started_at_ms finished_at_ms elapsed_ms spans sql timeline timeline_dropped
             outgoing_message_id http_status thread_cpu_ms process_gc_ms process_allocated_objects]
verify(Rails.logger.events.all? { |event| (event.keys - allowed).empty? &&
                                      event['event'] == 'nevidimy_atomic_turn_timing' }, 'event allowlist')
verify(!JSON.generate(Rails.logger.events).include?('PRIVATE_SENTINEL'), 'sensitive sentinel logged')
report[:content_free_allowlist] = true

controller_class.new(private_message: true).create
verify(!Rails.logger.events.last.key?('outgoing_message_id'), 'private note reported as public reply')
controller_class.new(gate: -> { NevidimyTurnTiming.record_message(Message.new(77, private_message: true)) }).create
verify(Rails.logger.events.last['outgoing_message_id'] == 123, 'private note shadowed public reply timing')

opaque = Object.new
opaque.define_singleton_method(:to_json) { raise 'event must not be serialized' }
opaque.define_singleton_method(:inspect) { raise 'event must not be inspected' }
ui, bot = ActionCableListener.new, AgentBotListener.new
controller_class.new(gate: -> {
  verify(ui.message_created(opaque).equal?(opaque), 'UI native return changed')
  verify(ui.conversation_updated(opaque).equal?(opaque), 'UI update return changed')
  verify(bot.message_created(opaque).equal?(opaque), 'bot native return changed')
  verify(bot.conversation_updated(opaque).equal?(opaque), 'bot update return changed')
}).create
verify(NevidimyTurnTiming::LISTENER_SPANS.all? { |name|
  Rails.logger.events.last['spans'][name.to_s]['count'] == 1
}, 'listener attribution or reload count changed')
before = Rails.logger.events.length
verify(ui.message_created(opaque).equal?(opaque) && Rails.logger.events.length == before, 'unscoped UI event changed')
report[:listeners_preserve_arguments_returns_and_have_fixed_nested_spans] = true

ui.failure = failure
begin
  controller_class.new(gate: -> { ui.message_created(opaque) }).create
  raise 'listener exception swallowed'
rescue ProbeFailure => caught
  verify(caught.equal?(failure), 'listener exception identity changed')
end
verify(Rails.logger.events.last['spans']['ui_message_created']['count'] == 1 &&
       !Rails.logger.events.last.key?('http_status'), 'raising listener not measured')
ui.failure = nil
report[:listener_exceptions_and_scope_cleanup_remain_native] = true

input_job = Webhooks::TelegramEventsJob.new
before = Rails.logger.events.length
verify(input_job.perform(opaque) == :native_input_return && input_job.calls == 1 &&
       input_job.received.equal?(opaque), 'input arguments or return changed')
input_event = Rails.logger.events.last
verify(Rails.logger.events.length == before + 1 && input_event['event'] == 'nevidimy_telegram_input_timing' &&
       input_event['incoming_message_id'] == 456 && !input_event.key?('outgoing_message_id') &&
       !input_event.key?('http_status'), 'input event metadata changed')
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'input scope leaked')
report[:native_input_root_uses_only_integer_message_metadata] = true

input_job.failure = failure
begin
  input_job.perform(opaque)
  raise 'input exception swallowed'
rescue ProbeFailure => caught
  verify(caught.equal?(failure), 'input exception identity changed')
end
verify(input_job.calls == 2 && Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'input failure retried or leaked')
input_job.failure = nil
Rails.logger.failure = RuntimeError.new('PRIVATE_SENTINEL')
verify(input_job.perform(opaque) == :native_input_return && input_job.calls == 3, 'input logger changed native work')
Rails.logger.failure = nil
Thread.current[:probe_clock_failure] = true
before = Rails.logger.events.length
verify(input_job.perform(opaque) == :native_input_return && input_job.calls == 4 &&
       Rails.logger.events.length == before, 'input clock failure changed native work')
Thread.current[:probe_clock_failure] = nil
report[:input_failure_logger_and_clock_do_not_change_processing] = true

before = Rails.logger.events.length
controller_class.new(gate: -> { input_job.perform(opaque) }).create
verify(Rails.logger.events.length == before + 1 && Rails.logger.events.last['event'] == 'nevidimy_atomic_turn_timing',
       'nested input replaced parent scope')
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY].nil?, 'nested input leaked scope')
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = false
input_job.perform(opaque)
verify(Thread.current[NevidimyTurnTiming::SCOPE_KEY] == false, 'inactive previous input scope changed')
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = nil
allowed << 'incoming_message_id'
verify(Rails.logger.events.all? { |event| (event.keys - allowed).empty? &&
       %w[nevidimy_atomic_turn_timing nevidimy_telegram_input_timing].include?(event['event']) }, 'input event allowlist')
verify(!JSON.generate(Rails.logger.events).include?('PRIVATE_SENTINEL'), 'input sensitive sentinel logged')
report[:input_nesting_and_content_free_allowlist] = true

Thread.current[:probe_monotonic_values] = [10.0, 10.1, 10.2, 10.4, 10.7, 11.0]
Thread.current[:probe_wall_values] = [1_700_000_000_000, 1_700_000_001_000]
Thread.current[:probe_resource_counters] = {
  cpu: [1_000_000_000, 1_010_000_000, 1_020_000_000, 1_040_000_000, 1_070_000_000, 1_100_000_000],
  gc: [0, 0], allocations: [0, 0]
}
scope = NevidimyTurnTiming.start
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = scope
begin
  result = NevidimyTurnTiming.measure(:message_builder) do
    ActiveSupport::Notifications.emit(ProbeSqlPayload.new, 0.001)
    NevidimyTurnTiming.measure(:ui_message_created) do
      ActiveSupport::Notifications.emit(ProbeSqlPayload.new('SCHEMA'), 0.002)
      :nested_return
    end
  end
  verify(result == :nested_return, 'timeline changed nested return')
ensure
  Thread.current[NevidimyTurnTiming::SCOPE_KEY] = nil
end
NevidimyTurnTiming.finish(scope, nil, completed: true)
waterfall = Rails.logger.events.last
parent, child = waterfall['timeline']
verify(waterfall['started_at_ms'] == 1_700_000_000_000 && waterfall['finished_at_ms'] == 1_700_000_001_000 &&
       waterfall['elapsed_ms'] == 1000.0, 'absolute correlation or monotonic root duration changed')
verify(parent == { 'span' => 'message_builder', 'start_ms' => 100.0, 'end_ms' => 700.0,
                   'thread_cpu_ms' => 60.0, 'sql_count' => 2, 'sql_elapsed_ms' => 3.0 } &&
       child == { 'span' => 'ui_message_created', 'start_ms' => 200.0, 'end_ms' => 400.0,
                  'thread_cpu_ms' => 20.0, 'sql_count' => 1, 'sql_elapsed_ms' => 2.0 },
       'nested timeline ordering, inclusive CPU or SQL deltas changed')
verify(waterfall['spans']['message_builder'] == { 'count' => 1, 'elapsed_ms' => 600.0 } &&
       waterfall['spans']['ui_message_created'] == { 'count' => 1, 'elapsed_ms' => 200.0 } &&
       waterfall['thread_cpu_ms'] == 100.0 && waterfall['timeline_dropped'].zero?, 'nested metrics summed or aggregate schema changed')
Thread.current[:probe_monotonic_values] = nil
Thread.current[:probe_wall_values] = nil
Thread.current[:probe_resource_counters] = nil
report[:timeline_has_absolute_root_times_and_ordered_inclusive_span_deltas] = true

Thread.current[:probe_cpu_reads] = 0
scope = NevidimyTurnTiming.start
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = scope
begin
  (NevidimyTurnTiming::TIMELINE_LIMIT + 3).times do
    NevidimyTurnTiming.measure(:send_reply) { ActiveSupport::Notifications.emit(ProbeSqlPayload.new) }
  end
ensure
  Thread.current[NevidimyTurnTiming::SCOPE_KEY] = nil
end
NevidimyTurnTiming.finish(scope, nil, completed: true)
bounded = Rails.logger.events.last
verify(bounded['timeline'].length == 64 && bounded['timeline_dropped'] == 3 &&
       bounded['spans']['send_reply']['count'] == 67 && bounded['sql']['uncached']['count'] == 67,
       'timeline overflow lost native aggregates or exceeded bound')
verify(Thread.current[:probe_cpu_reads] == 130, 'timeline overflow kept sampling extra resource counters')
Thread.current[:probe_cpu_reads] = nil
report[:timeline_limit_drops_only_detail_and_bounds_resource_sampling] = true

scope = NevidimyTurnTiming.start
scope[:timeline].freeze
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = scope
begin
  verify(NevidimyTurnTiming.measure(:send_reply) { :native_return } == :native_return, 'timeline allocation failure changed return')
  begin
    NevidimyTurnTiming.measure(:send_reply) { raise failure }
  rescue ProbeFailure => caught
    verify(caught.equal?(failure), 'timeline failure replaced native exception')
  end
ensure
  Thread.current[NevidimyTurnTiming::SCOPE_KEY] = nil
end
NevidimyTurnTiming.finish(scope, nil, completed: true)
verify(Rails.logger.events.last['timeline'].empty? && Rails.logger.events.last['timeline_dropped'] == 2 &&
       Rails.logger.events.last['spans']['send_reply']['count'] == 2, 'failed detail lost original span aggregates')

scope = NevidimyTurnTiming.start
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = scope
begin
  verify(NevidimyTurnTiming.measure(:send_reply) { scope[:timeline].last.freeze; :native_return } == :native_return,
         'timeline recording failure changed return')
  begin
    NevidimyTurnTiming.measure(:send_reply) { scope[:timeline].last.freeze; raise failure }
  rescue ProbeFailure => caught
    verify(caught.equal?(failure), 'timeline recording failure replaced native exception')
  end
  verify(NevidimyTurnTiming.measure('PRIVATE_SENTINEL') { :native_return } == :native_return, 'unknown span changed return')
ensure
  Thread.current[NevidimyTurnTiming::SCOPE_KEY] = nil
end
NevidimyTurnTiming.finish(scope, nil, completed: true)
verify(Rails.logger.events.last['timeline'].empty? && Rails.logger.events.last['timeline_dropped'] == 2 &&
       Rails.logger.events.last['spans']['send_reply']['count'] == 2, 'partial or unknown detail was exported')
report[:timeline_failure_and_unknown_spans_never_change_native_work] = true

scope = NevidimyTurnTiming.start
Thread.current[NevidimyTurnTiming::SCOPE_KEY] = scope
begin
  NevidimyTurnTiming.measure(:send_reply) { :native_return }
ensure
  Thread.current[NevidimyTurnTiming::SCOPE_KEY] = nil
end
scope[:timeline].first[:unsafe] = opaque
scope[:timeline].first[:thread_cpu_ms] = Float::NAN
scope[:timeline] << { span: :send_reply, start_ms: 0.0, end_ms: Float::INFINITY, sql_count: 0, sql_elapsed_ms: 0.0 }
NevidimyTurnTiming.finish(scope, nil, completed: true)
sanitized = Rails.logger.events.last
verify(sanitized['timeline'].length == 1 && sanitized['timeline_dropped'] == 1 &&
       !sanitized['timeline'].first.key?('thread_cpu_ms') && !sanitized['timeline'].first.key?('unsafe'),
       'malformed or unrecognized detail was exported')

timeline_fields = %w[span start_ms end_ms sql_count sql_elapsed_ms thread_cpu_ms]
verify(Rails.logger.events.all? { |event|
  event['started_at_ms'].is_a?(Integer) && event['finished_at_ms'].is_a?(Integer) &&
    event['started_at_ms'].positive? && event['finished_at_ms'].positive? &&
    event['timeline'].length <= 64 && event['timeline_dropped'].is_a?(Integer) && event['timeline_dropped'] >= 0 &&
    event['timeline'].all? { |row|
      (row.keys - timeline_fields).empty? && NevidimyTurnTiming::SPANS.map(&:to_s).include?(row['span']) &&
        row.reject { |key, _value| key == 'span' }.values.all? { |value| value.is_a?(Numeric) && value.finite? && value >= 0 } &&
        row['sql_count'].is_a?(Integer) && row['end_ms'] >= row['start_ms'] && row['end_ms'] <= event['elapsed_ms']
    }
}, 'timeline numeric or fixed-key allowlist')
verify(!JSON.generate(Rails.logger.events).include?('PRIVATE_SENTINEL'), 'timeline exposed sensitive metadata')
report[:timeline_exports_only_bounded_fixed_names_and_safe_numbers] = true

puts JSON.generate(report)

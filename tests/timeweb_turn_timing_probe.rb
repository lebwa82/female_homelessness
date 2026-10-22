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

  def initialize(id, failure: nil, fail_at: nil, outgoing: true)
    @id, @failure, @fail_at, @outgoing = id, failure, fail_at, outgoing
    @order = []
  end

  def outgoing?
    @outgoing
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
                         builder_failure: nil, gate: nil, outgoing: true)
            @params = atomic ? { nevidimy_atomic_turn: 'PRIVATE_SENTINEL' } : {}
            @response = OpenStruct.new(status: 201)
            @native_message = Message.new(id, failure: failure, fail_at: fail_at, outgoing: outgoing)
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
verify(first['spans'].keys.sort == (['message_builder'] + Message::CALLBACK_ORDER.map(&:to_s)).sort,
       'span allowlist')
verify(first['spans'].values.all? { |value| value['count'] == 1 && value['elapsed_ms'] >= 0 }, 'native spans')
verify(cold.native_message.order == Message::CALLBACK_ORDER, 'native callback ordering changed')
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

class << NevidimyTurnTiming
  alias_method :probe_real_monotonic, :monotonic

  def monotonic
    raise 'PRIVATE_SENTINEL' if Thread.current[:probe_clock_failure]

    probe_real_monotonic
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

load initializer
2.times { Rails.application.reloader.prepare }
verify(ActiveSupport::Notifications.subscriptions.length == 1, 'reload duplicated SQL subscriber')
before = Rails.logger.events.length
controller_class.new.create
verify(Rails.logger.events.length == before + 1, 'reload duplicated controller wrapper')
verify(Rails.logger.events.last['spans'].values.all? { |value| value['count'] == 1 }, 'reload duplicated native wrapper')
verify(Message.private_instance_methods.include?(:send_reply), 'private callback visibility changed')
report[:reload_idempotency] = true

controller_class.new(id: 'PRIVATE_SENTINEL').create
verify(!Rails.logger.events.last.key?('outgoing_message_id'), 'noninteger message id logged')
controller_class.new(outgoing: false).create
verify(!Rails.logger.events.last.key?('outgoing_message_id'), 'nonoutgoing message id logged')
invalid_status = controller_class.new
invalid_status.response.status = 'PRIVATE_SENTINEL'
invalid_status.create
verify(!Rails.logger.events.last.key?('http_status'), 'invalid status logged')
allowed = %w[event elapsed_ms spans sql outgoing_message_id http_status]
verify(Rails.logger.events.all? { |event| (event.keys - allowed).empty? &&
                                      event['event'] == 'nevidimy_atomic_turn_timing' }, 'event allowlist')
verify(!JSON.generate(Rails.logger.events).include?('PRIVATE_SENTINEL'), 'sensitive sentinel logged')
report[:content_free_allowlist] = true

puts JSON.generate(report)

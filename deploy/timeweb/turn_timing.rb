# Content-free attribution of native atomic-create and Telegram input work.
# Observation only: native callbacks, delivery, exceptions and ordering are unchanged.
require 'json'

module NevidimyTurnTiming
  SCOPE_KEY = :nevidimy_atomic_turn_timing unless const_defined?(:SCOPE_KEY, false)
  CALLBACKS = %i[
    reopen_conversation mark_pending_conversation_as_open_for_human_response
    set_conversation_activity dispatch_create_events send_reply
    execute_message_template_hooks update_contact_activity
  ].freeze unless const_defined?(:CALLBACKS, false)
  LISTENER_EVENTS = %i[message_created conversation_updated].freeze unless const_defined?(:LISTENER_EVENTS, false)
  LISTENER_SPANS = %i[ui_message_created ui_conversation_updated bot_message_created bot_conversation_updated].freeze unless const_defined?(:LISTENER_SPANS, false)
  SPANS = ([:message_builder] + CALLBACKS + LISTENER_SPANS).freeze unless const_defined?(:SPANS, false)
  SQL_KINDS = %i[schema cached uncached].freeze unless const_defined?(:SQL_KINDS, false)

  def self.monotonic
    Process.clock_gettime(Process::CLOCK_MONOTONIC)
  end

  def self.thread_cpu_time
    Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID, :nanosecond)
  rescue StandardError, NotImplementedError
    nil
  end

  def self.process_gc_time
    GC.total_time
  rescue StandardError, NotImplementedError
    nil
  end

  def self.process_allocated_objects
    GC.stat(:total_allocated_objects)
  rescue StandardError, NotImplementedError
    nil
  end

  def self.counter_delta(started, finished, unit = 1)
    return unless started.is_a?(Integer) && finished.is_a?(Integer) && started >= 0 && finished >= started

    value = (finished - started) / unit
    value.round(3) if value.finite?
  end

  def self.start
    scope = {
      started: monotonic,
      spans: SPANS.to_h { |name| [name, { count: 0, elapsed_ms: 0.0 }] },
      sql: SQL_KINDS.to_h { |name| [name, { count: 0, elapsed_ms: 0.0 }] }
    }
    scope[:thread_cpu_started] = thread_cpu_time
    scope[:process_gc_started] = process_gc_time
    scope[:process_allocated_started] = process_allocated_objects
    scope
  rescue StandardError
    nil
  end

  def self.measure(name)
    scope = Thread.current[SCOPE_KEY]
    started = monotonic if scope
  rescue StandardError
    yield
  else
    begin
      yield
    ensure
      record_span(scope, name, started) if scope
    end
  end

  def self.record_span(scope, name, started)
    bucket = scope[:spans].fetch(name)
    bucket[:count] += 1
    bucket[:elapsed_ms] += (monotonic - started) * 1000
  rescue StandardError
    # Telemetry must not replace a native return value or exception.
    nil
  end

  def self.record_message(message)
    scope = Thread.current[SCOPE_KEY]
    return unless scope && !message.private? && message.id.is_a?(Integer) && message.id.positive?

    if scope[:input_job] == true
      scope[:incoming_message_id] ||= message.id if message.incoming?
    elsif message.outgoing?
      scope[:outgoing_message_id] ||= message.id
    end
  rescue StandardError
    nil
  end

  def self.sql(_name, started, finished, _id, payload)
    scope = Thread.current[SCOPE_KEY]
    return unless scope

    # The name is compared only against a fixed classification value; it and all
    # SQL text, binds, connection metadata and exception fields are never retained.
    kind = payload[:name] == 'SCHEMA' ? :schema : (payload[:cached] ? :cached : :uncached)
    elapsed = (finished - started) * 1000
    return unless elapsed.finite? && elapsed >= 0

    scope[:sql][kind][:count] += 1
    scope[:sql][kind][:elapsed_ms] += elapsed
  rescue StandardError
    nil
  end

  def self.counters(values, names)
    names.to_h do |name|
      value = values.fetch(name)
      [name, { count: value.fetch(:count), elapsed_ms: value.fetch(:elapsed_ms).round(3) }]
    end
  end

  def self.finish(scope, controller, completed:)
    thread_cpu_ms = counter_delta(scope[:thread_cpu_started], thread_cpu_time, 1_000_000.0)
    # These two counters cover the whole process, not just this request when
    # threads overlap. Do not enable/change GC accounting or trigger collection.
    process_gc_ms = counter_delta(scope[:process_gc_started], process_gc_time, 1_000_000.0)
    process_allocated = counter_delta(scope[:process_allocated_started], process_allocated_objects)
    # Reconstruct the event from fixed keys; never serialize request/model data.
    event = {
      event: scope[:input_job] == true ? 'nevidimy_telegram_input_timing' : 'nevidimy_atomic_turn_timing',
      elapsed_ms: ((monotonic - scope.fetch(:started)) * 1000).round(3),
      spans: counters(scope.fetch(:spans), SPANS),
      sql: counters(scope.fetch(:sql), SQL_KINDS)
    }
    event[:thread_cpu_ms] = thread_cpu_ms unless thread_cpu_ms.nil?
    event[:process_gc_ms] = process_gc_ms unless process_gc_ms.nil?
    event[:process_allocated_objects] = process_allocated unless process_allocated.nil?
    id_field = scope[:input_job] == true ? :incoming_message_id : :outgoing_message_id
    message_id = scope[id_field]
    event[id_field] = message_id if message_id.is_a?(Integer) && message_id.positive?
    # On an exception, middleware may still change the final HTTP status.
    status = controller.response.status if completed && controller
    event[:http_status] = status if status.is_a?(Integer) && status.between?(100, 599)
    Rails.logger.info(event.to_json)
  rescue StandardError
    nil
  end

  module Controller
    def create
      return super unless params.key?(:nevidimy_atomic_turn)

      previous = Thread.current[SCOPE_KEY]
      scope = NevidimyTurnTiming.start
      return super unless scope

      Thread.current[SCOPE_KEY] = scope
      begin
        super
      ensure
        # Restore before logging too: logger/database work is outside this scope.
        Thread.current[SCOPE_KEY] = previous
        NevidimyTurnTiming.finish(scope, self, completed: $!.nil?)
      end
    end
  end

  module Builder
    def perform
      result = NevidimyTurnTiming.measure(:message_builder) { super }
      NevidimyTurnTiming.record_message(result)
      result
    end
  end

  module TelegramInput
    def perform(params = {})
      previous = Thread.current[SCOPE_KEY]
      return super if previous

      scope = NevidimyTurnTiming.start
      return super unless scope

      scope[:input_job] = true
      Thread.current[SCOPE_KEY] = scope
      begin
        super
      ensure
        Thread.current[SCOPE_KEY] = previous
        NevidimyTurnTiming.finish(scope, nil, completed: $!.nil?)
      end
    end
  end

  module MessageCallbacks
    CALLBACKS.each do |name|
      define_method(name) do
        NevidimyTurnTiming.record_message(self) if name == :dispatch_create_events
        NevidimyTurnTiming.measure(name) { super() }
      end
      private name
    end
  end

  module UiEvents
    LISTENER_EVENTS.each do |name|
      define_method(name) do |event|
        NevidimyTurnTiming.measure(:"ui_#{name}") { super(event) }
      end
    end
  end

  module BotEvents
    LISTENER_EVENTS.each do |name|
      define_method(name) do |event|
        NevidimyTurnTiming.measure(:"bot_#{name}") { super(event) }
      end
    end
  end

  def self.install_sql_subscriber
    @sql_subscriber ||= ActiveSupport::Notifications.monotonic_subscribe('sql.active_record', method(:sql))
  rescue StandardError
    # Missing telemetry must not prevent Rails startup or native message handling.
    nil
  end
end

NevidimyTurnTiming.install_sql_subscriber
Rails.application.reloader.to_prepare do
  controller = Api::V1::Accounts::Conversations::MessagesController
  controller.prepend(NevidimyTurnTiming::Controller) unless controller < NevidimyTurnTiming::Controller
  builder = Messages::MessageBuilder
  builder.prepend(NevidimyTurnTiming::Builder) unless builder < NevidimyTurnTiming::Builder
  Message.prepend(NevidimyTurnTiming::MessageCallbacks) unless Message < NevidimyTurnTiming::MessageCallbacks
  [[ActionCableListener, NevidimyTurnTiming::UiEvents], [AgentBotListener, NevidimyTurnTiming::BotEvents]].each do |listener, wrapper|
    next unless NevidimyTurnTiming::LISTENER_EVENTS.all? { |name|
      listener.public_method_defined?(name) && listener.instance_method(name).arity == 1
    }

    listener.prepend(wrapper) unless listener < wrapper
  end
  job = Webhooks::TelegramEventsJob
  if job.public_method_defined?(:perform) && job.instance_method(:perform).arity == -1 &&
     !(job < NevidimyTurnTiming::TelegramInput)
    job.prepend(NevidimyTurnTiming::TelegramInput)
  end
end

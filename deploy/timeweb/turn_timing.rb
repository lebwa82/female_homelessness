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
  COMPACT_GET_METHODS = {
    authenticate_by_access_token?: :auth_mode, authenticate_access_token!: :auth_token,
    validate_bot_access_token!: :auth_bot_endpoint, current_account: :account,
    validate_token_api_access: :account_api_access, check_account_mfa_enforcement: :account_mfa,
    conversation: :conversation_policy, show: :projection
  }.freeze unless const_defined?(:COMPACT_GET_METHODS, false)
  COMPACT_GET_SPANS = ([:process_action] + COMPACT_GET_METHODS.values).freeze unless const_defined?(:COMPACT_GET_SPANS, false)
  SQL_KINDS = %i[schema cached uncached].freeze unless const_defined?(:SQL_KINDS, false)
  TIMELINE_LIMIT = 64 unless const_defined?(:TIMELINE_LIMIT, false)

  def self.wall_ms
    (Time.now.to_f * 1000).to_i
  end

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

  def self.start(compact_get: false)
    names = compact_get ? COMPACT_GET_SPANS : SPANS
    scope = {
      started: monotonic, started_at_ms: wall_ms, timeline: [], timeline_dropped: 0,
      spans: names.to_h { |name| [name, { count: 0, elapsed_ms: 0.0 }] },
      sql: SQL_KINDS.to_h { |name| [name, { count: 0, elapsed_ms: 0.0 }] }
    }
    scope[:compact_get] = true if compact_get
    scope[:thread_cpu_started] = thread_cpu_time
    scope[:process_gc_started] = process_gc_time
    scope[:process_allocated_started] = process_allocated_objects
    scope
  rescue StandardError, NotImplementedError
    nil
  end

  def self.span_names(scope)
    scope[:compact_get] == true ? COMPACT_GET_SPANS : SPANS
  end

  def self.measure(name)
    scope = Thread.current[SCOPE_KEY]
    started = monotonic if scope
    snapshot = start_span(scope, name, started) if scope
  rescue StandardError, NotImplementedError
    yield
  else
    begin
      yield
    ensure
      record_span(scope, name, started, snapshot) if scope
    end
  end

  def self.sql_totals(scope)
    values = SQL_KINDS.map { |kind| scope.fetch(:sql).fetch(kind) }
    [values.sum { |value| value.fetch(:count) }, values.sum { |value| value.fetch(:elapsed_ms) }]
  end

  def self.start_span(scope, name, started)
    return unless span_names(scope).include?(name)
    if scope.fetch(:timeline).length >= TIMELINE_LIMIT
      scope[:timeline_dropped] += 1
      return
    end

    offset = (started - scope.fetch(:started)) * 1000
    raise ArgumentError unless offset.finite? && offset >= 0

    entry = { span: name, start_ms: offset.round(3) }
    snapshot = { entry: entry, cpu: thread_cpu_time, sql: sql_totals(scope) }
    # Reserve in start order, so enclosing spans precede their nested work.
    scope[:timeline] << entry
    snapshot
  rescue StandardError
    scope[:timeline_dropped] += 1 if scope.is_a?(Hash) && scope[:timeline_dropped].is_a?(Integer)
    nil
  end

  def self.record_span(scope, name, started, snapshot = nil)
    finished = monotonic
    elapsed = (finished - started) * 1000
    return unless elapsed.finite? && elapsed >= 0

    bucket = scope[:spans].fetch(name)
    bucket[:count] += 1
    bucket[:elapsed_ms] += elapsed
    return unless snapshot

    cpu = counter_delta(snapshot[:cpu], thread_cpu_time, 1_000_000.0)
    count, sql_elapsed = sql_totals(scope).zip(snapshot[:sql]).map { |current, previous| current - previous }
    offset = (finished - scope.fetch(:started)) * 1000
    return unless offset.finite? && offset.round(3) >= snapshot[:entry][:start_ms] &&
                  count.is_a?(Integer) && count >= 0 && sql_elapsed.finite? && sql_elapsed >= 0

    snapshot[:entry].merge!(end_ms: offset.round(3), sql_count: count, sql_elapsed_ms: sql_elapsed.round(3))
    snapshot[:entry][:thread_cpu_ms] = cpu unless cpu.nil?
  rescue StandardError, NotImplementedError
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

  def self.record_input_commit(message)
    scope = Thread.current[SCOPE_KEY]
    return unless scope && scope[:input_job] == true && !scope[:input_commit_seen] &&
                  message.incoming? && !message.private? && message.id.is_a?(Integer) && message.id.positive?

    scope[:input_commit_seen] = true
    elapsed = (monotonic - scope.fetch(:started)) * 1000
    return unless elapsed.finite? && elapsed >= 0

    snapshot = { elapsed_ms: elapsed.round(3), sql: scope.fetch(:sql).transform_values(&:dup) }
    cpu = counter_delta(scope[:thread_cpu_started], thread_cpu_time, 1_000_000.0)
    snapshot[:thread_cpu_ms] = cpu unless cpu.nil?
    scope[:before_first_after_commit] = snapshot
  rescue StandardError, NotImplementedError
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

  def self.timeline(scope)
    scope.fetch(:timeline).first(TIMELINE_LIMIT).select do |entry|
      entry.is_a?(Hash) && span_names(scope).include?(entry[:span]) &&
        %i[start_ms end_ms sql_elapsed_ms].all? { |key| entry[key].is_a?(Numeric) && entry[key].finite? && entry[key] >= 0 } &&
        entry[:end_ms] >= entry[:start_ms] && entry[:sql_count].is_a?(Integer) && entry[:sql_count] >= 0
    end.map do |entry|
      # Never export snapshot baselines or fields supplied by native work.
      row = entry.slice(:span, :start_ms, :end_ms, :thread_cpu_ms, :sql_count, :sql_elapsed_ms)
      cpu = row[:thread_cpu_ms]
      row.delete(:thread_cpu_ms) unless cpu.is_a?(Numeric) && cpu.finite? && cpu >= 0
      row
    end
  end

  def self.finish(scope, controller, completed:)
    thread_cpu_ms = counter_delta(scope[:thread_cpu_started], thread_cpu_time, 1_000_000.0)
    # These two counters cover the whole process, not just this request when
    # threads overlap. Do not enable/change GC accounting or trigger collection.
    process_gc_ms = counter_delta(scope[:process_gc_started], process_gc_time, 1_000_000.0)
    process_allocated = counter_delta(scope[:process_allocated_started], process_allocated_objects)
    timeline = timeline(scope)
    # Reconstruct the event from fixed keys; never serialize request/model data.
    event = {
      event: scope[:compact_get] == true ? 'nevidimy_compact_get_timing' :
        (scope[:input_job] == true ? 'nevidimy_telegram_input_timing' : 'nevidimy_atomic_turn_timing'),
      started_at_ms: scope.fetch(:started_at_ms), finished_at_ms: wall_ms,
      elapsed_ms: ((monotonic - scope.fetch(:started)) * 1000).round(3),
      spans: counters(scope.fetch(:spans), span_names(scope)),
      sql: counters(scope.fetch(:sql), SQL_KINDS), timeline: timeline,
      timeline_dropped: scope.fetch(:timeline_dropped) + scope.fetch(:timeline).length - timeline.length
    }
    event[:thread_cpu_ms] = thread_cpu_ms unless thread_cpu_ms.nil?
    event[:process_gc_ms] = process_gc_ms unless process_gc_ms.nil?
    event[:process_allocated_objects] = process_allocated unless process_allocated.nil?
    id_field = scope[:input_job] == true ? :incoming_message_id : :outgoing_message_id
    message_id = scope[id_field]
    event[id_field] = message_id if scope[:compact_get] != true && message_id.is_a?(Integer) && message_id.positive?
    snapshot = scope[:before_first_after_commit]
    if scope[:input_job] == true && snapshot.is_a?(Hash) &&
       snapshot[:elapsed_ms].is_a?(Numeric) && snapshot[:elapsed_ms].finite? && snapshot[:elapsed_ms] >= 0 &&
       SQL_KINDS.all? { |kind|
         value = snapshot.fetch(:sql).fetch(kind)
         value[:count].is_a?(Integer) && value[:count] >= 0 &&
           value[:elapsed_ms].is_a?(Numeric) && value[:elapsed_ms].finite? && value[:elapsed_ms] >= 0
       }
      detail = { elapsed_ms: snapshot[:elapsed_ms], sql: counters(snapshot.fetch(:sql), SQL_KINDS) }
      cpu = snapshot[:thread_cpu_ms]
      detail[:thread_cpu_ms] = cpu if cpu.is_a?(Numeric) && cpu.finite? && cpu >= 0
      event[:before_first_after_commit] = detail
    end
    # On an exception, middleware may still change the final HTTP status.
    status = controller.response.status if completed && controller
    event[:http_status] = status if status.is_a?(Integer) && status.between?(100, 599)
    Rails.logger.info(event.to_json)
  rescue StandardError, NotImplementedError
    nil
  end

  def self.compact_get?(controller, arguments)
    return false unless arguments.first == 'show' && controller.request.get?

    # Observe the literal technical key only. Parsing params before native auth
    # could move a malformed-query exception ahead of the authentication gate.
    query = controller.request.query_string
    query.is_a?(String) && query.match?(/(?:\A|&)nevidimy_turn_state(?:=|&|\z)/)
  rescue StandardError, NotImplementedError
    false
  end

  module CompactGetController
    def process_action(*arguments, &block)
      return super unless NevidimyTurnTiming.compact_get?(self, arguments)

      previous = Thread.current[SCOPE_KEY]
      scope = NevidimyTurnTiming.start(compact_get: true)
      return super unless scope

      Thread.current[SCOPE_KEY] = scope
      begin
        NevidimyTurnTiming.measure(:process_action) { super(*arguments, &block) }
      ensure
        Thread.current[SCOPE_KEY] = previous
        NevidimyTurnTiming.finish(scope, self, completed: $!.nil?)
      end
    end

    COMPACT_GET_METHODS.each do |name, stage|
      define_method(name) do |*arguments, &block|
        scope = Thread.current[SCOPE_KEY]
        if scope.is_a?(Hash) && scope[:compact_get] == true
          NevidimyTurnTiming.measure(stage) { super(*arguments, &block) }
        else
          super(*arguments, &block)
        end
      end
    end
  end

  def self.install_compact_get(controller)
    return if controller < CompactGetController

    names = [:process_action] + COMPACT_GET_METHODS.keys
    return unless names.all? { |name| controller.method_defined?(name) || controller.private_method_defined?(name) }
    return unless COMPACT_GET_METHODS.keys.all? { |name| controller.instance_method(name).arity.zero? }

    names.each do |name|
      visibility = if controller.private_method_defined?(name)
        :private
      elsif controller.protected_method_defined?(name)
        :protected
      else
        :public
      end
      CompactGetController.send(visibility, name)
    end
    controller.prepend(CompactGetController)
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
        NevidimyTurnTiming.record_input_commit(self) if name == :reopen_conversation
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
  if defined?(Api::V1::Accounts::ConversationsController)
    NevidimyTurnTiming.install_compact_get(Api::V1::Accounts::ConversationsController)
  end
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

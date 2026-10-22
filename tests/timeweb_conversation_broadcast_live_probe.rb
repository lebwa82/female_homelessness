# Read-only A/B in a launcher-validated clone and an isolated synthetic API inbox.
# Capture every job; execute only the explicit native ActionCable job under a stub.
require 'json'
require 'logger'
require 'ostruct'
require 'active_job/queue_adapters/test_adapter'

class NevidimyConversationBroadcastLiveProbe
  class Failure < StandardError; end

  def check(value, code)
    raise Failure, code unless value
  end

  def run
    @adapters, @loggers = {}, {}
    quiet = Logger.new(File::NULL)
    [Rails, ActiveRecord::Base, ActiveJob::Base].each do |owner|
      @loggers[owner] = owner.logger
      owner.logger = quiet
    end
    @adapter = ActiveJob::QueueAdapters::TestAdapter.new
    @adapter.perform_enqueued_jobs = @adapter.perform_enqueued_at_jobs = false
    ([ActiveJob::Base] + ActiveJob::Base.descendants).uniq.each do |job|
      @adapters[job] = job.queue_adapter
      job.queue_adapter = @adapter
    end
    config = JSON.parse(ENV.fetch('NEVIDIMY_BROADCAST_PROBE'))
    check(config.is_a?(Hash) && config.keys.sort == %w[account_id conversation_id inbox_id run_id], 'config_keys')
    %w[account_id conversation_id inbox_id].each do |key|
      check(config[key].is_a?(Integer) && config[key].positive?, 'config_id')
    end
    check(config['run_id'].is_a?(String) && config['run_id'].match?(/\A[A-Za-z0-9_-]{8,128}\z/), 'config_run')
    initializer = '/run/nevidimy-check/conversation_broadcast_fast_path.rb'
    require initializer unless defined?(NevidimyConversationBroadcastFastPath)
    check(ActionCableListener < NevidimyConversationBroadcastFastPath::Listener, 'optimization_disabled_at_boot')

    @account = Account.find(config.fetch('account_id'))
    inbox = @account.inboxes.find(config.fetch('inbox_id'))
    check(inbox.channel_type == 'Channel::Api' &&
          inbox.name.start_with?('Nevidimy atomic turn verification '), 'inbox_identity')
    [inbox, inbox.channel].each do |record|
      check(record.attributes.none? { |key, value| key.match?(/(?:webhook|callback).*url/) && value.present? }, 'external_callback')
    end
    @conversation = @account.conversations.find_by!(display_id: config.fetch('conversation_id'), inbox_id: inbox.id)
    check(@conversation.custom_attributes['acceptance_run_id'] == config.fetch('run_id'), 'conversation_identity')

    @listener = ActionCableListener.instance
    @native = ActionCableListener.instance_method(:conversation_updated)
    if defined?(NevidimyTurnTiming::UiEvents)
      check(@native.owner == NevidimyTurnTiming::UiEvents, 'timing_not_outermost')
    end
    wrappers = [NevidimyConversationBroadcastFastPath::Listener]
    wrappers << NevidimyTurnTiming::UiEvents if defined?(NevidimyTurnTiming::UiEvents)
    owners = []
    until @native.owner == ActionCableListener
      check(wrappers.include?(@native.owner), 'unknown_listener_wrapper')
      owners << @native.owner
      @native = @native.super_method
      check(@native, 'native_callback_missing')
    end
    check(owners.include?(NevidimyConversationBroadcastFastPath::Listener), 'projection_not_in_call_chain')
    @native = @native.bind(@listener)
    @candidate = NevidimyConversationBroadcastFastPath::Listener.instance_method(:conversation_updated).bind(@listener)

    @original_server = ActionCable.method(:server)
    @broadcasts = []
    captured = @broadcasts
    server = Object.new
    server.define_singleton_method(:broadcast) do |member, payload|
      captured << [member, JSON.parse(ActiveSupport::JSON.encode(payload))]
    end
    ActionCable.define_singleton_method(:server) { server }

    samples = { baseline: [], candidate: [] }
    ActiveRecord::Base.transaction(requires_new: true) do
      ActiveRecord::Base.connection.execute('SET TRANSACTION READ ONLY')
      expected, = measure(false)
      candidate, = measure(true)
      check(candidate == expected, 'native_output_changed')
      10.times do |index|
        (index.even? ? [false, true] : [true, false]).each do |optimized|
          output, sample = measure(optimized)
          check(output == expected, 'native_output_drift')
          samples[optimized ? :candidate : :baseline] << sample
        end
      end
    end
    puts "BROADCAST_PROBE=#{JSON.generate(ok: true, native_output_equal: true, read_only: true,
                                         captured_jobs_only: true, samples: samples)}"
  rescue StandardError => error
    result = { ok: false, error_type: error.class.name }
    result[:check] = error.message if error.is_a?(Failure)
    result[:method_name] = error.name.to_s if error.is_a?(NoMethodError)
    result[:frames] = error.backtrace_locations.first(5).map do |frame|
      { file: File.basename(frame.path), line: frame.lineno }
    end
    puts "BROADCAST_PROBE=#{JSON.generate(result)}"
    exit 1
  ensure
    ActionCable.define_singleton_method(:server, @original_server) if @original_server
    @adapters&.each { |job, adapter| job.queue_adapter = adapter }
    @loggers&.each { |owner, logger| owner.logger = logger }
  end

  def metrics
    counts = { schema: 0, cached: 0, uncached: 0 }
    subscriber = ActiveSupport::Notifications.subscribe('sql.active_record') do |_, _, _, _, payload|
      kind = payload[:name] == 'SCHEMA' ? :schema : (payload[:cached] ? :cached : :uncached)
      counts[kind] += 1
    end
    cpu = Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID)
    allocated = GC.stat(:total_allocated_objects)
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    result = yield
    sample = { elapsed_ms: ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000).round(3),
               thread_cpu_ms: ((Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID) - cpu) * 1000).round(3),
               allocated_objects: GC.stat(:total_allocated_objects) - allocated, sql: counts }
    [result, sample]
  ensure
    ActiveSupport::Notifications.unsubscribe(subscriber) if subscriber
  end

  def measure(optimized)
    @adapter.enqueued_jobs.clear
    @adapter.performed_jobs.clear
    @broadcasts.clear
    output, sample = ActiveRecord::Base.cache do
      fresh = @account.conversations.find(@conversation.id)
      event = OpenStruct.new(data: {conversation: fresh})
      ActiveRecord::Base.connection.clear_query_cache
      _, listener_sample = metrics { (optimized ? @candidate : @native).call(event) }
      jobs = @adapter.enqueued_jobs
      check(jobs.length == 1 && jobs.first[:job] == ActionCableBroadcastJob, 'unexpected_enqueued_job')
      check(@adapter.performed_jobs.empty?, 'automatic_job_execution')
      # A real worker cannot reuse the caller's query cache. Include native
      # argument deserialization, including any now-discarded GlobalID reads.
      ActiveRecord::Base.connection.clear_query_cache
      _, worker_sample = metrics do
        arguments = ActiveJob::Arguments.deserialize(jobs.first.fetch(:args))
        check(arguments.length == 3 && arguments[1] == Events::Types::CONVERSATION_UPDATED, 'queued_event_changed')
        check(arguments[2][:account_id] == @account.id && arguments[2][:id] == @conversation.display_id, 'queued_scope_changed')
        ActionCableBroadcastJob.new.perform(*arguments)
      end
      check(@adapter.enqueued_jobs.length == 1 && @adapter.performed_jobs.empty?, 'unexpected_worker_jobs')
      [@broadcasts.map(&:dup), { listener: listener_sample, worker: worker_sample }]
    end
    [output, sample]
  end
end

NevidimyConversationBroadcastLiveProbe.new.run

# Clone-only, read-only A/B. Recipient bindings are synthetic; all jobs are
# captured by the native ActiveJob test adapter and none are performed.
require 'json'
require 'logger'
require 'ostruct'
require 'active_job/queue_adapters/test_adapter'

class NevidimyAgentBotConversationLiveProbe
  class Failure < StandardError; end

  def check(value, code)
    raise Failure, code unless value
  end

  def run
    @adapters, @loggers, @environment = {}, {}, {}
    [Rails, ActiveRecord::Base, ActiveJob::Base].each do |owner|
      @loggers[owner] = owner.logger
      owner.logger = Logger.new(File::NULL)
    end
    @adapter = ActiveJob::QueueAdapters::TestAdapter.new
    @adapter.perform_enqueued_jobs = @adapter.perform_enqueued_at_jobs = false
    ([ActiveJob::Base] + ActiveJob::Base.descendants).uniq.each do |job|
      @adapters[job] = job.queue_adapter
      job.queue_adapter = @adapter
    end
    check(ENV.fetch('POSTGRES_DATABASE').match?(/\Anevidimy_cw419_[0-9a-f]{12}\z/), 'clone_database')
    connection = ActiveRecord::Base.connection
    check(connection.select_value('SELECT current_database()') == ENV.fetch('POSTGRES_DATABASE'), 'database_identity')
    check(connection.select_value('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()') == true, 'database_tls')
    config = JSON.parse(ENV.fetch('NEVIDIMY_AGENT_BOT_CONVERSATION_PROBE'))
    check(config.is_a?(Hash) && config.keys.sort == %w[account_id conversation_id inbox_id run_id], 'config_keys')
    %w[account_id conversation_id inbox_id].each do |key|
      check(config[key].is_a?(Integer) && config[key].positive?, 'config_id')
    end
    check(config['run_id'].is_a?(String) && config['run_id'].match?(/\A[A-Za-z0-9_-]{8,128}\z/), 'config_run')
    require '/run/nevidimy-check/agent_bot_conversation_projection.rb' unless defined?(NevidimyAgentBotConversationProjection)
    @projection = NevidimyAgentBotConversationProjection
    @projection.install
    check(AgentBotListener < @projection::Listener && @projection.verified_contract, 'native_contract')
    @account = Account.find(config.fetch('account_id'))
    inbox = @account.inboxes.find(config.fetch('inbox_id'))
    check(inbox.channel_type == 'Channel::Api' &&
          inbox.name.start_with?('Nevidimy atomic turn verification '), 'inbox_identity')
    [inbox, inbox.channel].each do |record|
      check(record.attributes.none? { |key, value| key.match?(/(?:webhook|callback).*url/) && value.present? }, 'external_callback')
    end
    @conversation = @account.conversations.find_by!(display_id: config.fetch('conversation_id'), inbox_id: inbox.id)
    check(@conversation.custom_attributes['acceptance_run_id'] == config.fetch('run_id'), 'conversation_identity')
    [@projection::OPT_IN, 'NEVIDIMY_AGENT_WEBHOOK_URL'].each { |key| @environment[key] = ENV[key] }
    ENV[@projection::OPT_IN] = '1'
    approved = 'https://agent-bot:8080/webhooks/chatwoot/agent/' + ('p' * 32)
    ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved
    @internal = AgentBot.new(id: 2_000_000_001, name: 'projection probe', outgoing_url: approved, secret: 'probe-only')
    @external = AgentBot.new(id: 2_000_000_002, name: 'external projection probe',
                             outgoing_url: 'https://external.invalid/projection-probe', secret: 'probe-only')
    check(@projection.internal_recipient?(@internal), 'synthetic_recipient_guard')
    @listener = AgentBotListener.instance
    native = AgentBotListener.instance_method(:conversation_updated)
    native = native.super_method while native && native.owner != AgentBotListener
    check(native, 'native_callback_missing')
    @native = native.bind(@listener)
    @candidate = @projection::Listener.instance_method(:conversation_updated).bind(@listener)
    reports = {}
    ActiveRecord::Base.transaction(requires_new: true) do
      connection.execute('SET TRANSACTION READ ONLY')
      [false, true].each do |mixed|
        expected, = measure(false, mixed)
        candidate, = measure(true, mixed)
        verify_output(expected, candidate, mixed)
        samples = { baseline: [], candidate: [] }
        10.times do |index|
          (index.even? ? [false, true] : [true, false]).each do |optimized|
            output, sample = measure(optimized, mixed)
            if optimized
              verify_output(expected, output, mixed)
            else
              check(output == expected, 'native_output_drift')
            end
            samples[optimized ? :candidate : :baseline] << sample
          end
        end
        reports[mixed ? :mixed_recipients : :internal_only] = {
          samples: samples, essential_payload_equal: true, other_payload_equal: true, job_contract_equal: true,
          baseline_payload_keys: expected.map { |job| job.fetch(:payload).size },
          candidate_payload_keys: candidate.map { |job| job.fetch(:payload).size }
        }
      end
    end
    puts "AGENT_BOT_CONVERSATION_PROBE=#{JSON.generate(ok: true, read_only: true, synthetic_recipients: true,
                                                     captured_jobs_only: true, consumer_equal: true,
                                                     native_fanout_preserved: true, external_payload_equal: true,
                                                     job_contract_equal: true, source_guards_verified: true, cases: reports)}"
  rescue StandardError => error
    report = { ok: false, error_type: error.class.name }
    report[:check] = error.message if error.is_a?(Failure)
    report[:method_name] = error.name.to_s if error.is_a?(NoMethodError)
    report[:frames] = error.backtrace_locations.first(5).map { |frame| { file: File.basename(frame.path), line: frame.lineno } }
    puts "AGENT_BOT_CONVERSATION_PROBE=#{JSON.generate(report)}"
    exit 1
  ensure
    @environment&.each { |key, value| value.nil? ? ENV.delete(key) : ENV[key] = value }
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
    [result, { elapsed_ms: ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000).round(3),
               thread_cpu_ms: ((Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID) - cpu) * 1000).round(3),
               allocated_objects: GC.stat(:total_allocated_objects) - allocated, sql: counts }]
  ensure
    ActiveSupport::Notifications.unsubscribe(subscriber) if subscriber
  end

  def measure(optimized, mixed)
    @adapter.enqueued_jobs.clear
    @adapter.performed_jobs.clear
    ActiveRecord::Base.cache do
      conversation = @account.conversations.find(@conversation.id)
      inbox, internal, external = conversation.inbox, @internal, @external
      binding = OpenStruct.new(active?: true)
      # Exercise native agent_bots_for/active_inbox_agent_bot, not a replacement
      # listener method. These unsaved recipient bindings exist only in this run.
      inbox.define_singleton_method(:agent_bot_inbox) { binding }
      inbox.define_singleton_method(:agent_bot) { internal }
      conversation.define_singleton_method(:assignee_type) { mixed ? 'AgentBot' : nil }
      conversation.define_singleton_method(:ai_assignee) { mixed ? external : nil }
      check(@projection.current_contract?(@listener, conversation), 'receiver_contract')
      event = OpenStruct.new(data: { conversation: conversation,
                                    changed_attributes: { 'waiting_since' => [Time.utc(2026, 10, 22, 10), nil] } })
      ActiveRecord::Base.connection.clear_query_cache
      _, sample = metrics { (optimized ? @candidate : @native).call(event) }
      check(@adapter.performed_jobs.empty?, 'job_performed')
      jobs = @adapter.enqueued_jobs
      check(jobs.length == (mixed ? 2 : 1), 'recipient_count')
      output = jobs.map do |job|
        check(job[:job] == AgentBots::WebhookJob && job[:queue] == 'high', 'job_type_or_queue')
        args = ActiveJob::Arguments.deserialize(job.fetch(:args))
        check(args.length == 4 && args[2] == :agent_bot_webhook && args[3].is_a?(Hash), 'job_arguments')
        kwargs = args[3].dup
        delivery_id = kwargs.delete(:delivery_id)
        check(delivery_id.is_a?(String) && delivery_id.match?(/\A[0-9a-f-]{36}\z/), 'delivery_uuid')
        { target: args[0], payload: JSON.parse(ActiveSupport::JSON.encode(args[1])),
          kind: args[2], kwargs: kwargs, queue: job[:queue] }
      end
      [output, sample]
    end
  end

  def verify_output(baseline, candidate, mixed)
    check(baseline.length == candidate.length, 'job_count_changed')
    baseline.zip(candidate).each_with_index do |(before, after), index|
      check(before.reject { |key, _| key == :payload } == after.reject { |key, _| key == :payload }, 'job_contract_changed')
      if index.zero?
        essential = before[:payload].select { |key, _| %w[id event changed_attributes].include?(key) }
        check(after[:payload] == essential, 'consumer_essential_payload_changed')
      else
        check(mixed && before[:payload] == after[:payload], 'external_payload_changed')
      end
    end
  end
end

NevidimyAgentBotConversationLiveProbe.new.run

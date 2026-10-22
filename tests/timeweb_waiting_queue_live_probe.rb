# Existing isolated clone only: SELECTs, synthetic recipients, captured jobs.
require 'json'
require 'digest'
require 'logger'
require 'ostruct'
require 'net/http'
require 'active_job/queue_adapters/test_adapter'

class NevidimyWaitingQueueProbe
  class Failure < StandardError; end
  def check(value, code)
    raise Failure, code unless value
  end

  def run
    config = JSON.parse(ENV.fetch('NEVIDIMY_YJIT_PROBE'))
    @stage, @adapters, @loggers, @saved_env = 'identity', {}, {}, {}
    [Rails, ActiveRecord::Base, ActiveJob::Base].each { |owner| @loggers[owner] = owner.logger; owner.logger = Logger.new(File::NULL) }
    check(config['expected_database'].match?(/\Anevidimy_cw419_[a-f0-9]{12}\z/), 'clone_identity')
    connection = ActiveRecord::Base.connection
    check(connection.select_value('SELECT current_database()') == config['expected_database'], 'database_identity')
    check(connection.raw_connection.conninfo.any? { |row| row[:keyword] == 'sslmode' && row[:val] == 'verify-full' } &&
          connection.select_value('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()') == true, 'database_tls')
    Rails.application.eager_load!
    @adapter = ActiveJob::QueueAdapters::TestAdapter.new
    @adapter.perform_enqueued_jobs = @adapter.perform_enqueued_at_jobs = false
    ([ActiveJob::Base] + ActiveJob::Base.descendants).uniq.each { |job| @adapters[job] = job.queue_adapter; job.queue_adapter = @adapter }
    ActiveJob::Base.prepend(Module.new { def perform_now(*); raise NevidimyWaitingQueueProbe::Failure, 'job_execution'; end })
    Net::HTTP.prepend(Module.new { def request(*); raise NevidimyWaitingQueueProbe::Failure, 'http_execution'; end })
    check(Digest::SHA256.file('/run/nevidimy-check/candidate.rb').hexdigest == config['expected_candidate_sha256'], 'source_hash')
    load '/run/nevidimy-check/candidate.rb'
    projection = NevidimyAgentBotConversationProjection
    projection.install
    check(AgentBotListener < projection::Listener && projection.verified_contract, 'native_contract')
    [projection::OPT_IN, projection::BACKGROUND_WAITING, 'NEVIDIMY_AGENT_WEBHOOK_URL'].each { |key| @saved_env[key] = ENV[key] }
    ENV[projection::OPT_IN] = '1'
    approved = 'https://agent-bot:8080/webhooks/chatwoot/agent/' + ('p' * 32)
    ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved
    internal = AgentBot.new(id: 2_000_000_001, outgoing_url: approved, secret: 'synthetic-only')
    external = AgentBot.new(id: 2_000_000_002, outgoing_url: 'https://example.invalid/never-called', secret: 'synthetic-only')
    now, later = Time.utc(2026, 1, 1), Time.utc(2026, 1, 2)
    timer = { 'waiting_since' => [now, nil], 'updated_at' => [now, later] }
    cases = [timer, timer.merge('waiting_since' => [nil, now]), timer.reject { |key, _| key == 'updated_at' },
             timer.merge('status' => ['pending', 'open']), timer.merge('assignee_id' => [nil, 9]),
             timer.merge('team_id' => [nil, 9]), timer.merge('unknown' => [nil, true])]
    results = []
    @stage = 'read_only_comparison'
    ActiveRecord::Base.transaction(requires_new: true) do
      connection.execute('SET TRANSACTION READ ONLY')
      account = Account.find(config.fetch('account_id'))
      anchor = account.inboxes.find(config.fetch('inbox_id'))
      check(anchor.channel_type == 'Channel::Api' && anchor.name.start_with?('Nevidimy atomic turn verification ') && anchor.callback_webhook_url.blank?, 'anchor_identity')
      seed = account.conversations.find_by!(display_id: config.fetch('seed_conversation_id'), inbox_id: anchor.id)
      check(seed.custom_attributes['acceptance_run_id'] == config.fetch('seed_run_id'), 'seed_identity')
      [false, true].each do |mixed|
        cases.each_with_index do |delta, index|
          output = [false, true].map do |enabled|
            ENV[projection::BACKGROUND_WAITING] = enabled ? '1' : '0'
            conversation = account.conversations.find(seed.id)
            inbox = conversation.inbox
            binding = OpenStruct.new(active?: true)
            inbox.define_singleton_method(:agent_bot_inbox) { binding }
            inbox.define_singleton_method(:agent_bot) { internal }
            conversation.define_singleton_method(:assignee_type) { mixed ? 'AgentBot' : nil }
            conversation.define_singleton_method(:ai_assignee) { mixed ? external : nil }
            check(projection.current_contract?(AgentBotListener.instance, conversation), 'receiver_contract')
            @adapter.enqueued_jobs.clear
            AgentBotListener.instance.conversation_updated(OpenStruct.new(data: { conversation: conversation, changed_attributes: delta }))
            check(@adapter.performed_jobs.empty?, 'job_performed')
            jobs = @adapter.enqueued_jobs.map do |job|
              check(job[:job] == AgentBots::WebhookJob, 'job_class_changed')
              check(job['queue_name'] == job[:queue], 'serialized_queue_changed')
              args = ActiveJob::Arguments.deserialize(job.fetch(:args))
              check(args.length == 4 && args[2] == :agent_bot_webhook, 'job_signature_changed')
              options = args[3].dup
              delivery = options.delete(:delivery_id)
              check(delivery.is_a?(String) && delivery.match?(/\A[0-9a-f-]{36}\z/), 'delivery_identity')
              { queue: job[:queue], args: [args[0], JSON.parse(ActiveSupport::JSON.encode(args[1])), args[2], options] }
            end
            check(jobs.length == (mixed ? 2 : 1), 'job_count_changed')
            if enabled && index < 2
              captured = @adapter.enqueued_jobs.first
              retry_instance = ActiveJob::Base.deserialize(captured)
              @adapter.enqueued_jobs.clear
              retry_instance.retry_job(wait: 1)
              retry_capture = @adapter.enqueued_jobs
              check(retry_capture.length == 1 && retry_capture.first[:queue] == 'medium' &&
                    retry_capture.first['queue_name'] == 'medium' &&
                    retry_capture.first['arguments'] == captured['arguments'], 'retry_queue_or_arguments_changed')
              check(@adapter.performed_jobs.empty?, 'retry_performed')
            end
            jobs
          end
          before, after = output
          check(before.map { |job| job[:args] } == after.map { |job| job[:args] }, 'payload_or_secret_changed')
          check(before.all? { |job| job[:queue] == 'high' }, 'baseline_queue')
          check(after.first[:queue] == (index < 2 ? 'medium' : 'high') &&
                (!mixed || after.last[:queue] == 'high'), 'priority_scope_changed')
          results << { mixed: mixed, timer_only: index < 2, job_count: after.length, queues: after.map { |job| job[:queue] } }
        end
      end
    end
    puts "WAITING_QUEUE_PROBE=#{JSON.generate(ok: true, read_only: true, jobs_performed: 0, http_requests: 0,
      payload_secret_identity_equal: true, native_retry_job_preserved: true, cases: results)}"
  rescue StandardError => error
    report = { ok: false, stage: @stage, error_type: error.class.name }
    report[:check] = error.message if error.is_a?(Failure)
    report[:frames] = Array(error.backtrace_locations).first(4).map { |frame| { file: File.basename(frame.path), line: frame.lineno } }
    puts "WAITING_QUEUE_PROBE=#{JSON.generate(report)}"
    exit 1
  ensure
    @saved_env&.each { |key, value| ENV[key] = value }
    @adapters&.each { |job, adapter| job.queue_adapter = adapter }
    @loggers&.each { |owner, logger| owner.logger = logger }
  end
end

NevidimyWaitingQueueProbe.new.run

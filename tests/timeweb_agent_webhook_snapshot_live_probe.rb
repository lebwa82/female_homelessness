# Clone-only AR acceptance, NOT a delivery/end-to-end latency benchmark.
# Raw synthetic fixtures roll back. Only the private, read-only body helper runs.
require 'json'
require 'digest'
require 'logger'
require 'net/http'

class NevidimyWebhookSnapshotLiveProbe
  class Failure < StandardError; end
  CANDIDATE = '/run/nevidimy-check/candidate.rb'.freeze
  OPT_IN = 'NEVIDIMY_AGENT_BUTTON_SNAPSHOT'.freeze
  PREFIX = 'Nevidimy atomic turn verification '.freeze

  module NoJobExecution
    def perform_now(*)
      raise NevidimyWebhookSnapshotLiveProbe::Failure, 'job_execution_forbidden'
    end
  end
  module NoHttp
    def request(*)
      raise NevidimyWebhookSnapshotLiveProbe::Failure, 'http_forbidden'
    end
  end

  def check(value, code)
    raise Failure, code unless value
  end

  def configure
    @config = JSON.parse(ENV.fetch('NEVIDIMY_YJIT_PROBE'))
    keys = %w[expected_database account_id inbox_id seed_conversation_id seed_run_id run_id bot_id expected_yjit expected_candidate_sha256]
    check(@config.is_a?(Hash) && @config.keys.sort == keys.sort, 'config_shape')
    %w[account_id inbox_id seed_conversation_id bot_id].each do |key|
      check(@config[key].is_a?(Integer) && @config[key].positive?, 'config_id')
    end
    check(@config['expected_database'].is_a?(String) && @config['expected_database'].match?(/\Anevidimy_cw419_[a-f0-9]{12}\z/), 'clone_name')
    check(@config['run_id'].is_a?(String) && @config['run_id'].match?(/\A[A-Za-z0-9_-]{32,128}\z/), 'run_id')
    check(@config['seed_run_id'].is_a?(String) && @config['seed_run_id'].match?(/\A[A-Za-z0-9_-]{8,128}\z/) &&
          @config['seed_run_id'] != @config['run_id'], 'seed_run_id')
    check(@config['expected_yjit'] == false && @config['expected_candidate_sha256'].is_a?(String) &&
          @config['expected_candidate_sha256'].match?(/\A[0-9a-f]{64}\z/), 'candidate_config')
    check(File.read(Rails.root.join('VERSION_CW')).strip == '4.19.0' && RUBY_VERSION == '3.4.4' &&
          Rails::VERSION::STRING == '7.2.3.1' && defined?(RubyVM::YJIT) && !RubyVM::YJIT.enabled?, 'native_runtime')
    @tag = "nevidimy-snapshot-#{@config.fetch('run_id')}"
  end

  def protect_execution
    require 'active_job/queue_adapters/test_adapter'
    @adapter = ActiveJob::QueueAdapters::TestAdapter.new
    @adapter.perform_enqueued_jobs = @adapter.perform_enqueued_at_jobs = false
    ([ActiveJob::Base] + ActiveJob::Base.descendants).uniq.each do |job|
      @adapters[job] = job.queue_adapter
      job.queue_adapter = @adapter
    end
    ActiveJob::Base.prepend(NoJobExecution)
    Net::HTTP.prepend(NoHttp)
  end

  def validate_target(connection)
    check(connection.adapter_name == 'PostgreSQL' && connection.open_transactions.zero?, 'database_adapter')
    check(ActiveRecord::Base.connection_db_config.database == @config.fetch('expected_database') &&
          connection.select_value('SELECT current_database()') == @config.fetch('expected_database'), 'database_identity')
    ssl = connection.raw_connection.conninfo.find { |option| option[:keyword] == 'sslmode' }
    check(ssl && ssl[:val] == 'verify-full' && connection.select_value('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()') == true, 'database_tls')
    @account = Account.find(@config.fetch('account_id'))
    anchor = @account.inboxes.find(@config.fetch('inbox_id'))
    check(anchor.channel_type == 'Channel::Api' && anchor.channel.instance_of?(Channel::Api) &&
          anchor.name.start_with?(PREFIX) && anchor.name.length > PREFIX.length &&
          @account.inboxes.where(name: anchor.name).pluck(:id) == [anchor.id] && anchor.callback_webhook_url.blank?, 'anchor_identity')
    seed = @account.conversations.find_by(display_id: @config.fetch('seed_conversation_id'), inbox_id: anchor.id)
    check(seed && seed.custom_attributes['acceptance_run_id'] == @config.fetch('seed_run_id'), 'seed_identity')
    bot = AgentBot.find(@config.fetch('bot_id'))
    check(bot.account_id == @account.id && (seed.ai_assignee == bot ||
          seed.messages.where(message_type: :outgoing, sender_type: 'AgentBot', sender_id: bot.id).exists?), 'bot_identity')
    check(!Integrations::Hook.enabled.where(account_id: @account.id).exists? && !Webhook.where(account_id: @account.id).exists?, 'external_hooks')
    check(!Inbox.where(account_id: @account.id, name: @tag).exists? &&
          !Contact.where(account_id: @account.id, identifier: @tag).exists?, 'run_already_exists')
  end

  def insert(model, attributes)
    time = Time.utc(2020, 1, 1)
    id = model.insert_all!([attributes.merge(created_at: time, updated_at: time)], returning: %w[id]).rows.flatten.fetch(0)
    @created[model] << id
    id
  end

  def fixtures
    # Never create!/save a Telegram channel: native callbacks call Telegram.
    channel = insert(Channel::Telegram, account_id: @account.id, bot_name: 'neutral_snapshot_probe', bot_token: "invalid-neutral-#{@tag}")
    inbox = insert(Inbox, account_id: @account.id, channel_id: channel, channel_type: 'Channel::Telegram', name: @tag,
                   enable_auto_assignment: false, greeting_enabled: false, working_hours_enabled: false,
                   enable_email_collect: false, csat_survey_enabled: false)
    contact = insert(Contact, account_id: @account.id, name: 'Neutral snapshot probe', identifier: @tag,
                     contact_type: Contact.contact_types.fetch('lead'))
    link = insert(ContactInbox, contact_id: contact, inbox_id: inbox, source_id: @tag, pubsub_token: @tag)
    @attributes = { 'snapshot_probe_run_id' => @config.fetch('run_id'), 'reply_owner' => 'bot', 'ownership_version' => 3 }
    conversation = insert(Conversation, account_id: @account.id, inbox_id: inbox, contact_id: contact,
      contact_inbox_id: link, display_id: @account.conversations.maximum(:display_id).to_i + 1,
      status: Conversation.statuses.fetch('pending'), custom_attributes: @attributes)
    message = insert(Message, account_id: @account.id, inbox_id: inbox, conversation_id: conversation,
      sender_type: 'Contact', sender_id: contact, message_type: Message.message_types.fetch('incoming'),
      content_type: Message.content_types.fetch('text'), content: 'Neutral snapshot probe', processed_message_content: 'Neutral snapshot probe',
      private: false, source_id: @tag, content_attributes: { 'nevidimy_telegram_callback_data' => 'menu:neutral' })
    @conversation = Conversation.find(conversation)
    @payload = { event: 'message_created', id: message, message_type: 'incoming', private: false, content: 'Neutral snapshot probe',
      content_attributes: { nevidimy_telegram_callback_data: 'menu:neutral' },
      conversation: { id: @conversation.display_id, channel: 'Channel::Telegram' }, sender: { id: contact }, inbox: { id: inbox } }
  end

  def invoke(payload = @payload)
    counts = { select: 0, message: 0, conversation: 0 }
    sql = lambda do |*, data|
      next if data[:name] == 'SCHEMA'

      text = data[:sql]
      check(text.match?(/\ASELECT\b/i) && !data[:cached], 'helper_non_select_or_cached')
      counts[:select] += 1
      counts[:message] += 1 if text.include?('FROM "messages"')
      counts[:conversation] += 1 if text.include?('FROM "conversations"')
    end
    original = payload.to_json
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    body = ActiveRecord::Base.while_preventing_writes do
      ActiveSupport::Notifications.subscribed(sql, 'sql.active_record') do
        @trigger.send(:nevidimy_snapshot_body, original)
      end
    end
    check(payload.to_json == original, 'input_mutated')
    [body, counts, (Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000]
  end

  def prove
    ENV.delete(OPT_IN)
    body, off_sql, off_ms = invoke
    check(body == @payload.to_json && off_sql.values.all?(&:zero?), 'default_off')
    ENV[OPT_IN] = '1'
    body, on_sql, on_ms = invoke
    check(on_sql == { select: 2, message: 1, conversation: 1 }, 'two_exact_selects')
    state = JSON.parse(body).fetch('nevidimy_button_snapshot')
    check(state.keys.sort == %w[schema capability features input_id contact_id inbox_id version conversation].sort &&
      state['schema'] == 1 && state['capability'] == 'nevidimy-atomic-turn-v1' && state['features'] == ['private_notes_v1'] &&
      state['input_id'] == @payload[:id] && state['contact_id'] == @payload[:sender][:id] && state['inbox_id'] == @payload[:inbox][:id], 'schema_identity')
    expected = { 'id' => @conversation.display_id, 'status' => @conversation.status, 'custom_attributes' => @attributes,
      'assignee_id' => nil, 'team_id' => nil, 'meta' => { 'assignee' => nil, 'assignee_type' => 'AgentBot', 'team' => nil } }
    check(state['conversation'] == expected && state['version'] == NevidimyAtomicTurn.version(@conversation) &&
          state['version'].match?(/\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\z/), 'coherent_native_row_schema')
    changes = [->(p) { p[:id] = -1 }, ->(p) { p[:sender][:id] += 1 }, ->(p) { p[:inbox][:id] += 1 },
               ->(p) { p[:conversation][:id] += 1 }, ->(p) { p[:content_attributes][:nevidimy_telegram_callback_data] = 'other' }]
    changes.each do |change|
      value = @payload.deep_dup
      change.call(value)
      check(invoke(value).first == value.to_json, 'identity_tamper_not_fallback')
    end
    fresh = @attributes.merge('neutral_revision' => 2)
    Conversation.where(id: @conversation.id).update_all(custom_attributes: fresh, updated_at: @conversation.updated_at + 1)
    updated = JSON.parse(invoke.first).fetch('nevidimy_button_snapshot')
    check(updated['conversation']['custom_attributes'] == fresh && updated['version'] != state['version'], 'fresh_not_cached_row')
    Conversation.where(id: @conversation.id).update_all(custom_attributes: { 'large' => 'x' * 262_144 })
    check(invoke.first == @payload.to_json, 'oversize_not_fallback')
    check(@adapter.enqueued_jobs.empty? && @adapter.performed_jobs.empty?, 'job_enqueued_or_performed')
    { sql: { enabled: on_sql, disabled: off_sql }, relative_wall_ms: (on_ms - off_ms).round(3) }
  end

  def run
    @stage, @created, @adapters, @loggers = 'configure', Hash.new { |hash, key| hash[key] = [] }, {}, {}
    @previous_opt = ENV[OPT_IN]
    configure
    Rails.application.eager_load!
    protect_execution
    [Rails, ActiveRecord::Base, ActiveJob::Base].each { |owner| @loggers[owner] = owner.logger; owner.logger = Logger.new(File::NULL) }
    @stage = 'target'; validate_target(ActiveRecord::Base.connection)
    @stage = 'candidate'
    check(Digest::SHA256.file(CANDIDATE).hexdigest == @config.fetch('expected_candidate_sha256'), 'candidate_source')
    load CANDIDATE
    Webhooks::Trigger.prepend(NevidimyInternalAgentWebhook) unless Webhooks::Trigger < NevidimyInternalAgentWebhook
    @trigger = Webhooks::Trigger.new('https://invalid.invalid', {}, :agent_bot_webhook)
    check(@trigger.method(:nevidimy_snapshot_body).owner == NevidimyInternalAgentWebhook &&
          File.expand_path(@trigger.method(:nevidimy_snapshot_body).source_location.first) == File.expand_path(CANDIDATE), 'candidate_method')
    result = nil
    ActiveRecord::Base.transaction(requires_new: true) do
      @stage = 'fixtures'; fixtures
      @stage = 'proof'; result = prove
      raise ActiveRecord::Rollback
    end
    @stage = 'rollback'
    @created.each { |model, ids| check(!model.unscoped.where(id: ids).exists?, 'fixture_retained') }
    check(@adapter.enqueued_jobs.empty? && @adapter.performed_jobs.empty?, 'job_after_rollback')
    puts "SNAPSHOT_PROBE=#{JSON.generate({ ok: true, component: 'late_agent_webhook_snapshot', results: result,
      invariants: { fixtures_rolled_back: true, helper_preventing_writes: true, exact_native_schema: true,
        exact_identity: true, fresh_coherent_version: true, default_off: true, oversize_fallback: true, jobs_performed: 0, http_requests: 0 },
      excludes: %w[webhook_transport python_processing atomic_commit queue_delivery telegram_delivery] })}"
  rescue StandardError => error
    report = { ok: false, stage: @stage, error_type: error.class.name }
    report[:check] = error.message if error.is_a?(Failure)
    report[:frames] = Array(error.backtrace_locations).first(4).map { |frame| { file: File.basename(frame.path), line: frame.lineno } }
    puts "SNAPSHOT_PROBE=#{JSON.generate(report)}"
    exit 1
  ensure
    ENV[OPT_IN] = @previous_opt
    @adapters&.each { |job, adapter| job.queue_adapter = adapter }
    @loggers&.each { |owner, logger| owner.logger = logger }
  end
end

NevidimyWebhookSnapshotLiveProbe.new.run if defined?(Rails.application)

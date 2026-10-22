# Clone-only listener benchmark. New neutral fixtures roll back; no jobs/HTTP run.
require 'json'
require 'digest'
require 'logger'
require 'net/http'

class NevidimyAgentBotMessageLiveProbe
  class Failure < StandardError; end
  PAIRS = 10
  PREFIX = 'Nevidimy atomic turn verification '.freeze
  OPT_IN = 'NEVIDIMY_AGENT_MESSAGE_PROJECTION'.freeze
  CANDIDATE = '/run/nevidimy-check/candidate.rb'.freeze
  CONTENT = 'Neutral callback benchmark'.freeze
  CALLBACK = 'menu:neutral'.freeze

  module NoJobExecution
    def perform_now(*)
      raise NevidimyAgentBotMessageLiveProbe::Failure, 'job_execution_forbidden'
    end
  end

  module NoNetwork
    def request(*)
      raise NevidimyAgentBotMessageLiveProbe::Failure, 'http_forbidden'
    end
  end

  attr_reader :config

  def check(value, code)
    raise Failure, code unless value
  end

  def configure(raw)
    @config = JSON.parse(raw)
    keys = %w[expected_database account_id inbox_id seed_conversation_id seed_run_id run_id bot_id expected_yjit expected_candidate_sha256]
    check(@config.is_a?(Hash) && @config.keys.sort == keys.sort, 'config_shape')
    %w[account_id inbox_id seed_conversation_id bot_id].each do |key|
      check(@config[key].is_a?(Integer) && @config[key].positive?, 'config_id')
    end
    check(@config['expected_database'].is_a?(String) && @config['expected_database'].match?(/\Anevidimy_cw419_[a-f0-9]{12}\z/), 'clone_name')
    check(@config['run_id'].is_a?(String) && @config['run_id'].match?(/\A[A-Za-z0-9_-]{32,128}\z/), 'config_run')
    check(@config['seed_run_id'].is_a?(String) && @config['seed_run_id'].match?(/\A[A-Za-z0-9_-]{8,128}\z/) &&
          @config['run_id'] != @config['seed_run_id'], 'config_seed_run')
    check(@config['expected_yjit'] == false, 'yjit_must_be_off')
    check(@config['expected_candidate_sha256'].is_a?(String) &&
          @config['expected_candidate_sha256'].match?(/\A[0-9a-f]{64}\z/), 'candidate_sha_shape')
    @tag = "nevidimy-message-#{@config.fetch('run_id')}"
  rescue JSON::ParserError
    raise Failure, 'config_json'
  end

  def validate_database(connection, database:)
    check(connection.adapter_name == 'PostgreSQL' && connection.open_transactions.zero?, 'database_adapter')
    check(database == @config.fetch('expected_database') && connection.select_value('SELECT current_database()') == database, 'database_identity')
    tls = connection.raw_connection.conninfo.find { |option| option[:keyword] == 'sslmode' }
    check(tls && tls[:val] == 'verify-full' && connection.select_value('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()') == true, 'database_tls')
  end

  def validate_target
    @account = Account.find(@config.fetch('account_id'))
    anchor = @account.inboxes.find(@config.fetch('inbox_id'))
    check(anchor.channel_type == 'Channel::Api' && anchor.channel.instance_of?(Channel::Api) &&
          anchor.name.start_with?(PREFIX) && anchor.name.length > PREFIX.length, 'anchor_identity')
    check(@account.inboxes.where(name: anchor.name).pluck(:id) == [anchor.id], 'anchor_name')
    check(anchor.callback_webhook_url.blank?, 'anchor_callback')
    seed = @account.conversations.find_by(display_id: @config.fetch('seed_conversation_id'), inbox_id: anchor.id)
    check(seed && seed.custom_attributes['acceptance_run_id'] == @config.fetch('seed_run_id'), 'seed_identity')
    @bot = AgentBot.find(@config.fetch('bot_id'))
    check(@bot.account_id == @account.id && (seed.ai_assignee == @bot ||
          seed.messages.where(message_type: :outgoing, sender_type: 'AgentBot', sender_id: @bot.id).exists?), 'bot_identity')
    check(!Integrations::Hook.enabled.where(account_id: @account.id).exists? && !Webhook.where(account_id: @account.id).exists?, 'external_hooks')
    check(!@account.feature_enabled?('unread_count_for_filters') && !ChatwootApp.advanced_search_allowed?, 'external_writes')
    recipients = @account.administrators.pluck(:id)
    check(recipients.any? && !User.where(id: recipients, pubsub_token: [nil, '']).exists?, 'recipient_token_backfill')
    projection = NevidimyAgentBotConversationProjection
    check(ENV[projection::OPT_IN] == '1' && projection.internal_recipient?(@bot) &&
          projection.current_contract?(AgentBotListener.instance, seed), 'existing_projection_contract')
    check(ENV['NEVIDIMY_UNREAD_COUNT_FAST_PATH'] != '1', 'other_candidate_enabled')
    check(!Inbox.where(account_id: @account.id, name: @tag).exists? &&
          !Contact.where(account_id: @account.id, identifier: @tag).exists?, 'run_already_exists')
  end

  def capture_jobs
    require 'active_job/queue_adapters/test_adapter'
    @adapter = ActiveJob::QueueAdapters::TestAdapter.new
    @adapter.perform_enqueued_jobs = @adapter.perform_enqueued_at_jobs = false
    ([ActiveJob::Base] + ActiveJob::Base.descendants).uniq.each do |job|
      @adapters[job] = job.queue_adapter
      job.queue_adapter = @adapter
    end
    ActiveJob::Base.prepend(NoJobExecution)
    Net::HTTP.prepend(NoNetwork)
  end

  def core_models
    [Account, Inbox, Channel::Api, Channel::Telegram, AgentBot, AgentBotInbox, User, AccountUser,
     Integrations::Hook, Webhook, Contact, ContactInbox, Conversation, Message]
  end

  def row_digest(model, fence)
    Digest::SHA256.hexdigest(JSON.generate(model.unscoped.where('id <= ?', fence).reorder(:id).pluck(:id, Arel.sql('xmin::text'))))
  end

  def insert(model, attributes)
    id = model.insert_all!([attributes.merge(created_at: @time, updated_at: @time)], returning: %w[id]).rows.flatten.fetch(0)
    @created[model] << id
    id
  end

  def fixtures
    @time = Time.utc(2020, 1, 1)
    # Never call Channel::Telegram.create!: its callbacks contact Telegram.
    channel = insert(Channel::Telegram, account_id: @account.id, bot_name: 'neutral_clone_probe', bot_token: "invalid-neutral-#{@tag}")
    inbox = insert(Inbox, account_id: @account.id, channel_id: channel, channel_type: 'Channel::Telegram', name: @tag,
                   enable_auto_assignment: false, greeting_enabled: false, working_hours_enabled: false,
                   enable_email_collect: false, csat_survey_enabled: false)
    insert(AgentBotInbox, account_id: @account.id, inbox_id: inbox, agent_bot_id: @bot.id, status: AgentBotInbox.statuses.fetch('active'))
    @inbox = Inbox.find(inbox)
    @contact = Contact.create!(account: @account, name: 'Neutral callback benchmark', identifier: @tag, contact_type: :lead)
    @created[Contact] << @contact.id
    @link = ContactInbox.create!(contact: @contact, inbox: @inbox, source_id: @tag)
    @created[ContactInbox] << @link.id
    @conversation = Conversation.create!(account: @account, inbox: @inbox, contact: @contact, contact_inbox: @link,
      status: :pending, ai_assignee: @bot, last_activity_at: @time,
      custom_attributes: { 'message_probe_run_id' => @config.fetch('run_id'), 'reply_owner' => 'bot', 'ownership_version' => 3 })
    @created[Conversation] << @conversation.id
    @conversation.reload # Native display_id/uuid are normally loaded after commit, which this probe forbids.
    @message_id = insert(Message, account_id: @account.id, inbox_id: inbox, conversation_id: @conversation.id,
      sender_type: 'Contact', sender_id: @contact.id, message_type: Message.message_types.fetch('incoming'),
      content_type: Message.content_types.fetch('text'), content: CONTENT, processed_message_content: CONTENT,
      private: false, source_id: @tag, content_attributes: { 'nevidimy_telegram_callback_data' => CALLBACK })
    @adapter.enqueued_jobs.clear
  end

  def install_candidate
    check(File.read(Rails.root.join('VERSION_CW')).strip == '4.19.0' && RUBY_VERSION == '3.4.4' &&
          Rails::VERSION::STRING == '7.2.3.1', 'native_runtime')
    check(defined?(RubyVM::YJIT) && !RubyVM::YJIT.enabled?, 'unexpected_yjit')
    check(Digest::SHA256.file(CANDIDATE).hexdigest == @config.fetch('expected_candidate_sha256'), 'candidate_source')
    load CANDIDATE
    ENV[OPT_IN] = '1'
    check(NevidimyAgentBotMessageProjection.install && AgentBotListener < NevidimyAgentBotMessageProjection::Listener, 'candidate_install')
  end

  def event(message, name = Events::Types::MESSAGE_CREATED, extra = {})
    Events::Base.new(name, Time.current, { message: message, performed_by: nil }.merge(extra))
  end

  def dispatch_diagnostics(receiver, name, type)
    actual, expected = receiver.method(name), type.instance_method(name)
    { bound_identity: actual == expected.bind(receiver), owner_equal: actual.owner == expected.owner,
      source_equal: actual.source_location == expected.source_location, arity_equal: actual.arity == expected.arity,
      original_name_equal: actual.original_name == expected.original_name }
  end

  def contract_diagnostics(listener, message)
    projection = NevidimyAgentBotMessageProjection
    serializers = projection.instance_variable_get(:@serializers)
    callback = projection.instance_variable_get(:@callback_method)
    policies = projection.instance_variable_get(:@projection_methods)
    details = {
      listener_created: projection.same_chain?(listener, :message_created, projection.instance_variable_get(:@listener_chain)),
      listener_delivery: projection.same_chain?(listener, :process_message_event, projection.instance_variable_get(:@message_event_chain)),
      callback_class_identity: AgentBotListener.instance_method(:nevidimy_project_callback) == callback,
      callback_dispatch: projection.same_dispatch?(listener.method(:nevidimy_project_callback), callback),
      serializer_class_identities: serializers.all? { |(type, name), original| type.instance_method(name) == original },
      recipient_policies: policies.all? { |name, original| NevidimyAgentBotConversationProjection.method(name) == original },
      self_filter_policy: NevidimyAgentBotSelfEventFilter.method(:skip?) == projection.instance_variable_get(:@self_filter_method),
      existing_projection: NevidimyAgentBotConversationProjection.current_contract?(listener, message.conversation)
    }
    [[message, Message, :webhook_data, :message_data], [message, Message, :webhook_content, :message_content],
     [message, Message, :webhook_sendable?, :message_gate], [message.sender, Contact, :webhook_data, :sender_data],
     [message.inbox, Inbox, :webhook_data, :inbox_data]].each do |receiver, type, name, key|
      details[key] = receiver.instance_of?(type) && projection.same_dispatch?(receiver.method(name), serializers.fetch([type, name]))
    end
    details
  rescue StandardError
    { diagnostic_available: false }
  end

  def prove_guards
    message, listener = Message.find(@message_id), AgentBotListener.instance
    projection = NevidimyAgentBotMessageProjection
    @guard_diagnostics = {
      candidate_installed: AgentBotListener < projection::Listener, callback_scope: projection.callback?(message),
      current_contract: projection.current_contract?(listener, message),
      existing_projection: NevidimyAgentBotConversationProjection.current_contract?(listener, message.conversation),
      self_filter_installed: AgentBotListener < NevidimyAgentBotSelfEventFilter::Listener,
      one_approved_recipient: listener.send(:agent_bots_for, message.inbox, message.conversation) == [@bot],
      message_dispatch: dispatch_diagnostics(message, :webhook_data, Message),
      sender_dispatch: dispatch_diagnostics(message.sender, :webhook_data, Contact),
      contract_components: contract_diagnostics(listener, message)
    }
    check(@guard_diagnostics.values_at(:candidate_installed, :callback_scope, :current_contract, :existing_projection,
                                     :self_filter_installed, :one_approved_recipient).all?, 'candidate_contract_before_benchmark')
    message.singleton_class # Deliberately harmless: never define a singleton method.
    @guard_diagnostics[:harmless_singleton_contract] = projection.current_contract?(listener, message)
    check(@guard_diagnostics[:harmless_singleton_contract], 'harmless_singleton_contract')
    outgoing = Message.new(account: @account, inbox: @inbox, conversation: @conversation, sender: @bot,
      message_type: :outgoing, private: false, content_attributes: { 'bot_turn_key' => "message:#{@message_id}" })
    @guard_diagnostics[:existing_self_filter_predicate] = outgoing.webhook_sendable? &&
      NevidimyAgentBotSelfEventFilter.skip?('message_created', @bot, outgoing)
    check(@guard_diagnostics[:existing_self_filter_predicate], 'existing_self_filter_predicate')
    @adapter.enqueued_jobs.clear
    listener.message_created(event(outgoing))
    check(@adapter.enqueued_jobs.empty?, 'existing_self_filter_inactive')
    listener.conversation_updated(event(nil, Events::Types::CONVERSATION_UPDATED,
      conversation: @conversation, changed_attributes: { waiting_since: [nil, @time] }))
    jobs = unpack_jobs
    check(jobs.length == 1 && jobs.first[:job] == 'AgentBots::WebhookJob' &&
          jobs.first[:payload].keys.map(&:to_s).sort == %w[changed_attributes event id], 'existing_projection_inactive')
    @adapter.enqueued_jobs.clear
  end

  def unpack_jobs
    check(@adapter.enqueued_jobs.length <= 32 && @adapter.performed_jobs.empty? &&
          !@adapter.perform_enqueued_jobs && !@adapter.perform_enqueued_at_jobs, 'job_execution_or_capture_limit')
    @adapter.enqueued_jobs.map do |job|
      args = ActiveJob::Arguments.deserialize(job.fetch(:args))
      if job.fetch(:job).name == 'AgentBots::WebhookJob'
        options = args.fetch(3)
        delivery = options[:delivery_id] || options['delivery_id']
        check(delivery.is_a?(String) && delivery.match?(/\A[0-9a-f-]{36}\z/), 'delivery_uuid')
        options = options.reject { |key, _| key.to_s == 'delivery_id' }
        { job: job.fetch(:job).name, queue: job.fetch(:queue), payload: args.fetch(1), contract: [args[0], args[2], options] }
      else
        { job: job.fetch(:job).name, queue: job.fetch(:queue), contract: args }
      end
    end
  end

  def sample(mode, compact: true)
    ENV[OPT_IN] = mode == :candidate ? '1' : '0'
    @adapter.enqueued_jobs.clear
    message = Message.find(@message_id)
    ActiveRecord::Base.connection.clear_query_cache
    sql = { schema: 0, cached: 0, uncached: 0, elapsed_ms: 0.0 }
    subscriber = nil
    ActiveRecord::Base.uncached do
      ActionCableListener.instance.message_created(event(message)) # Real native UI association priming, not timed.
      @guard_diagnostics[:sample_current_contract] = NevidimyAgentBotMessageProjection.current_contract?(AgentBotListener.instance, message)
      check(@guard_diagnostics[:sample_current_contract], 'sample_contract')
      subscriber = ActiveSupport::Notifications.monotonic_subscribe('sql.active_record') do |_, started, finished, _, payload|
        kind = payload[:name] == 'SCHEMA' ? :schema : payload[:cached] ? :cached : :uncached
        sql[kind] += 1
        sql[:elapsed_ms] += (finished - started) * 1000
      end
      wall, cpu, allocated = Process.clock_gettime(Process::CLOCK_MONOTONIC), Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID), GC.stat(:total_allocated_objects)
      AgentBotListener.instance.message_created(event(message))
      metrics = { wall_ms: (Process.clock_gettime(Process::CLOCK_MONOTONIC) - wall) * 1000,
        cpu_ms: (Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID) - cpu) * 1000,
        allocated_objects: GC.stat(:total_allocated_objects) - allocated, sql: sql }
      ActiveSupport::Notifications.unsubscribe(subscriber)
      subscriber = nil
      jobs = unpack_jobs
      bots = jobs.select { |job| job[:job] == 'AgentBots::WebhookJob' }
      check(bots.length == (compact ? 1 : 2), 'native_fanout_count')
      if compact && mode == :candidate
        check(bots.first[:payload] == NevidimyAgentBotMessageProjection.payload(message), 'candidate_fell_back')
      end
      [jobs, metrics]
    end
  ensure
    ActiveSupport::Notifications.unsubscribe(subscriber) if subscriber
  end

  def read(hash, key)
    hash.key?(key) ? hash[key] : hash[key.to_s]
  end

  def consumer_view(payload)
    conversation, sender, inbox = %i[conversation sender inbox].map { |name| read(payload, name) }
    attributes = read(payload, :content_attributes)
    check(read(payload, :content) == CONTENT && read(sender, :id) == @contact.id &&
          read(conversation, :id) == @conversation.display_id && read(inbox, :id) == @inbox.id &&
          read(attributes, :nevidimy_telegram_callback_data) == CALLBACK, 'non_neutral_consumer_fixture')
    { event: read(payload, :event), id: read(payload, :id), message_type: read(payload, :message_type), private: read(payload, :private),
      content: CONTENT, content_attributes: { nevidimy_telegram_callback_data: CALLBACK },
      conversation: { id: read(conversation, :id), channel: read(conversation, :channel) },
      sender: { id: read(sender, :id) }, inbox: { id: read(inbox, :id) } }
  end

  def quantile(values, fraction)
    values.sort[[(values.length * fraction).ceil - 1, 0].max]
  end

  def summarize(rows)
    %i[wall_ms cpu_ms allocated_objects].to_h { |key|
      values = rows.map { |row| row.fetch(key) }
      [key, { p50: quantile(values, 0.5).round(3), p95: quantile(values, 0.95).round(3) }]
    }.merge(sql: %i[schema cached uncached elapsed_ms].to_h { |key|
      values = rows.map { |row| row.fetch(:sql).fetch(key) }
      [key, { p50: quantile(values, 0.5).round(3), p95: quantile(values, 0.95).round(3) }]
    })
  end

  def compare
    rows, reference, consumer = { native: [], candidate: [] }, nil, {}
    (PAIRS + 2).times do |index|
      (index.even? ? %i[native candidate] : %i[candidate native]).each do |mode|
        @mode = mode.to_s
        jobs, metrics = sample(mode)
        contracts = jobs.map { |job| job.reject { |key, _| key == :payload } }
        reference ||= contracts
        check(contracts == reference, 'job_or_ui_contract_changed')
        consumer[mode] = consumer_view(jobs.find { |job| job[:job] == 'AgentBots::WebhookJob' }.fetch(:payload))
        rows.fetch(mode) << metrics if index >= 2
      end
    end
    @consumer_cases = [{ name: 'single_internal', baseline: consumer.fetch(:native), projected: consumer.fetch(:candidate) }]
    # A second real recipient must retain the native per-recipient full payload.
    external = insert(AgentBot, account_id: @account.id, name: 'Neutral external benchmark', outgoing_url: 'https://example.invalid/never-called')
    @conversation.update_columns(assignee_agent_bot_id: external, ai_assignee_type: 'AgentBot')
    native, = sample(:native, compact: false)
    candidate, = sample(:candidate, compact: false)
    check(native == candidate, 'mixed_native_payload_or_job_changed')
    { pairs: PAIRS, query_cache: 'disabled_for_both', native: summarize(rows[:native]), candidate: summarize(rows[:candidate]),
      job_ui_contract_equal: true, mixed_native_equal: true, single_recipient_compact_proven: true }
  end

  def unchanged
    @before.each { |model, value| check(row_digest(model, value[:fence]) == value[:digest], 'preexisting_row_changed') }
    check(@adapter.performed_jobs.empty?, 'job_performed')
  end

  def run
    @stage, @adapters, @loggers, @created = 'configure', {}, {}, Hash.new { |hash, key| hash[key] = [] }
    @old_opt = ENV[OPT_IN]
    configure(ENV.fetch('NEVIDIMY_YJIT_PROBE'))
    [Rails, ActiveRecord::Base, ActiveJob::Base].each { |owner| @loggers[owner] = owner.logger; owner.logger = Logger.new(File::NULL) }
    validate_database(ActiveRecord::Base.connection, database: ActiveRecord::Base.connection_db_config.database)
    Rails.application.eager_load!
    capture_jobs
    @current = %i[user account account_user executed_by contact inbox].to_h { |key| [key, Current.public_send(key)] }
    Current.reset
    @stage = 'target'; validate_target
    @stage = 'candidate'; install_candidate
    @before = core_models.to_h { |model| fence = model.unscoped.maximum(:id).to_i; [model, { fence: fence, digest: row_digest(model, fence) }] }
    results = nil
    ActiveRecord::Base.transaction(requires_new: true) do
      @stage = 'fixtures'; fixtures
      @stage = 'guards'; prove_guards
      @stage = 'benchmark'; results = compare
      unchanged
      raise ActiveRecord::Rollback
    end
    @stage = 'rollback'; unchanged
    @created.each { |model, ids| check(!model.unscoped.where(id: ids).exists?, 'fixture_retained') }
    puts "AGENT_BOT_MESSAGE_PROBE=#{JSON.generate(ok: true, component: 'agent_bot_message_created', results: results,
      guard_diagnostics: @guard_diagnostics, consumer_scope: 'synthetic_neutral_fixtures_only', consumer_cases: @consumer_cases,
      fixtures_created: @created.to_h { |model, ids| [model.name, ids.length] },
      invariants: { fixtures_rolled_back: true, preexisting_core_rows_unchanged: true, candidate_active: true,
        existing_projection_active: true, existing_self_filter_active: true, jobs_performed: 0, http_requests: 0 },
      excludes: %w[ingress message_creation commit queue_transport webhook_delivery telegram_delivery])}"
  rescue StandardError => error
    result = { ok: false, stage: @stage, mode: @mode, error_type: error.class.name, guard_diagnostics: @guard_diagnostics || {} }
    result[:check] = error.message if error.is_a?(Failure)
    result[:frames] = Array(error.backtrace_locations).first(4).map { |frame| { file: File.basename(frame.path), line: frame.lineno } }
    puts "AGENT_BOT_MESSAGE_PROBE=#{JSON.generate(result)}"
    exit 1
  ensure
    ENV[OPT_IN] = @old_opt
    @current&.each { |key, value| Current.public_send("#{key}=", value) }
    @adapters&.each { |job, adapter| job.queue_adapter = adapter }
    @loggers&.each { |owner, logger| owner.logger = logger }
  end
end

NevidimyAgentBotMessageLiveProbe.new.run if defined?(Rails.application)

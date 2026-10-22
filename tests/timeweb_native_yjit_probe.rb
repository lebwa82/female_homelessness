# Clone-only component benchmark, never a production runner or an end-to-end test.
# The launcher owns the pinned image, isolated clone lifecycle and YJIT env flags.
# Keep native commits/callbacks/serialization; capture jobs without performing any.
require 'json'
require 'logger'
require 'digest'

class NevidimyNativeYjitProbe
  class Failure < StandardError; end

  WARMUP_PAIRS = 100
  SAMPLE_PAIRS = 30
  PREFIX = 'Nevidimy atomic turn verification '.freeze
  SOURCE_SHA256S = {
    'app/builders/messages/message_builder.rb' => 'fe5674b397ddd0e4500ea2c6b7aa935bc9c7814d84b1d7fa1d6f77f75fc40afa',
    'app/models/message.rb' => 'a4e604d536e6d39a75e393b906b77a833d4baccadd4bae2d6f0477017d497015',
    'app/models/conversation.rb' => 'b09e0e2a58a334c91e7c8678fcd04d3608cc8bb83cae773001d97853a2d16820',
    'app/models/contact.rb' => '75ec0685efaa21b65f47a6845640bc5216bc44ba4441ccad494f156a8e312711',
    'app/dispatchers/dispatcher.rb' => '7dabff950c2d00eb0b8a4e3da826522c08daa09e5124511b629b5c725e8c80e2',
    'app/dispatchers/sync_dispatcher.rb' => '6b0f2ca2346c7f2e966c8cf5e0d3da5c732981da7da2428b1ecd588c49d509b8',
    'enterprise/app/dispatchers/enterprise/sync_dispatcher.rb' => '2dc155eb3d1e6f64d2cf833de8ea5d11cd2b739efd209dde95b46ac35d03c810',
    'enterprise/app/listeners/captain/conversation_outcome_event_listener.rb' => 'd348ab2fd3fc4a840d6891b75fd1fbf1f1b88cfdbfbb69081fe96c57cf65b91a',
    'enterprise/app/services/captain/conversation_outcome_tracker.rb' => 'a515a3f97a456f58983ce40f8620d4730c1a13d7da0cba8fd75d89fd0f47ccfa',
    'app/services/message_templates/hook_execution_service.rb' => 'b94ee3e74e63bca0ec76b62caf70533cb581f9e813dd464c6beb42191875cf38'
  }.freeze
  YJIT_STATS = %i[inline_code_size outlined_code_size freed_code_size code_region_size
                  live_page_count freed_page_count yjit_alloc_size compiled_iseq_count
                  compiled_block_count yjit_insns_count vm_insns_count ratio_in_yjit].freeze

  # TestAdapter settings alone must not permit a stray native perform_now.
  module NoJobExecution
    def perform_now(*)
      raise NevidimyNativeYjitProbe::Failure, 'job_execution_forbidden'
    end
  end

  attr_reader :config

  def check(value, code)
    raise Failure, code unless value
  end

  def configure(raw)
    @config = JSON.parse(raw)
    keys = %w[expected_database account_id inbox_id seed_conversation_id seed_run_id run_id bot_id expected_yjit]
    check(@config.is_a?(Hash) && @config.keys.sort == keys.sort, 'config_shape')
    %w[account_id inbox_id seed_conversation_id bot_id].each do |key|
      check(@config[key].is_a?(Integer) && @config[key].positive?, 'config_id')
    end
    check(@config['expected_database'].is_a?(String) &&
          @config['expected_database'].match?(/\Anevidimy_cw419_[a-f0-9]{12}\z/), 'clone_name')
    check(@config['run_id'].is_a?(String) && @config['run_id'].match?(/\A[A-Za-z0-9_-]{32,128}\z/), 'config_run')
    check(@config['seed_run_id'].is_a?(String) &&
          @config['seed_run_id'].match?(/\A[A-Za-z0-9_-]{8,128}\z/) &&
          @config['run_id'] != @config['seed_run_id'], 'config_seed_run')
    check([true, false].include?(@config['expected_yjit']), 'config_yjit')
    @tag = "nevidimy-yjit-#{@config.fetch('run_id')}"
  rescue JSON::ParserError
    raise Failure, 'config_json'
  end

  def validate_database(connection, database:)
    check(connection.adapter_name == 'PostgreSQL' && connection.open_transactions.zero?, 'database_adapter_or_transaction')
    check(database == @config.fetch('expected_database') &&
          connection.select_value('SELECT current_database()') == database, 'database_identity')
    # Native database.yml leaves TLS to libpq/PGSSLMODE, so AR's config hash
    # need not contain sslmode. Read only this allowlisted effective option.
    tls = connection.raw_connection.conninfo.find { |option| option[:keyword] == 'sslmode' }
    check(tls && tls[:val] == 'verify-full' &&
          connection.select_value('SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()') == true, 'database_tls')
  end

  def run
    @stage = 'configure'
    @adapters, @loggers, @created = {}, {}, Hash.new { |hash, key| hash[key] = [] }
    configure(ENV.fetch('NEVIDIMY_YJIT_PROBE'))
    silence
    database_config = ActiveRecord::Base.connection_db_config
    validate_database(ActiveRecord::Base.connection, database: database_config.database)
    @stage = 'native_contract'
    check(File.read(Rails.root.join('VERSION_CW')).strip == '4.19.0', 'native_version')
    check(RUBY_VERSION == '3.4.4' && Rails::VERSION::STRING == '7.2.3.1', 'runtime_version')
    SOURCE_SHA256S.each do |path, digest|
      check(Digest::SHA256.file(Rails.root.join(path)).hexdigest == digest, 'native_source')
    end
    check(defined?(RubyVM::YJIT) && RubyVM::YJIT.enabled? == @config.fetch('expected_yjit'), 'yjit_mode')
    Rails.application.eager_load!
    capture_jobs
    @current = %i[user account account_user executed_by contact inbox].to_h { |key| [key, Current.public_send(key)] }
    Current.reset
    @stage = 'target_guards'
    validate_target
    @stage = 'before_invariants'
    @before_rows = core_models.to_h do |model|
      fence = model.unscoped.maximum(:id).to_i
      [model, { fence: fence, digest: row_digest(model, fence) }]
    end
    @stage = 'synthetic_contact'
    prepare_contact
    @adapter.enqueued_jobs.clear
    @stage = 'warmup'
    memory_before = memory
    stats_before = yjit_stats
    WARMUP_PAIRS.times { |index| pair(index) }
    memory_after_warmup = memory
    stats_after_warmup = yjit_stats
    @stage = 'samples'
    samples = SAMPLE_PAIRS.times.map { |index| pair(WARMUP_PAIRS + index) }
    memory_after_samples = memory
    stats_after_samples = yjit_stats
    @stage = 'after_invariants'
    verify_records
    check_jobs
    puts "YJIT_PROBE=#{JSON.generate(
      ok: true, component: 'native_message_builder_pair', native_version: '4.19.0',
      ruby_version: RUBY_VERSION, yjit_enabled: RubyVM::YJIT.enabled?,
      warmup_pairs: WARMUP_PAIRS, sample_pairs: SAMPLE_PAIRS, samples: samples,
      memory: { before_warmup: memory_before, after_warmup: memory_after_warmup, after_samples: memory_after_samples },
      yjit_stats: { before_warmup: stats_before, after_warmup: stats_after_warmup, after_samples: stats_after_samples },
      invariants: { clone_database_exact: true, pg_verify_full: true, captured_jobs_only: true,
                    performed_jobs: 0, preexisting_core_rows_unchanged: true,
                    new_contacts: 1, new_contact_inboxes: 1, new_conversations: @created[Conversation].length,
                    new_messages: @created[Message].length, retained_tagged_records: true },
      excludes: %w[controller_auth http_ingress real_queue_transport queue_wait job_execution telegram_delivery]
    )}"
  rescue StandardError => error
    result = { ok: false, stage: @stage, error_type: error.class.name }
    result[:check] = error.message if error.is_a?(Failure)
    result[:method_name] = error.name.to_s if error.is_a?(NoMethodError)
    result[:frames] = Array(error.backtrace_locations).first(5).map do |frame|
      { file: File.basename(frame.path), line: frame.lineno }
    end
    puts "YJIT_PROBE=#{JSON.generate(result)}"
    exit 1
  ensure
    @adapter&.enqueued_jobs&.clear
    @current&.each { |key, value| Current.public_send("#{key}=", value) }
    @adapters&.each { |job, adapter| job.queue_adapter = adapter }
    @loggers&.each { |owner, logger| owner.logger = logger }
  end

  def silence
    quiet = Logger.new(File::NULL)
    [Rails, ActiveRecord::Base, ActionController::Base, ActiveJob::Base].each do |owner|
      @loggers[owner] = owner.logger
      owner.logger = quiet
    end
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
  end

  def validate_sync_listeners(dispatcher)
    # The pinned EE dispatcher appends Captain's outcome listener. Its pinned
    # tracker rejects incoming/AgentBot replies before writes; waiting_since
    # updates do not enter its status-resolved branch. Do not skip the listener.
    expected = [ActionCableListener.instance, AgentBotListener.instance, Captain::ConversationOutcomeEventListener.instance]
    check(dispatcher.listeners == expected, 'sync_listeners')
  end

  def validate_target
    @account = Account.find(@config.fetch('account_id'))
    @inbox = @account.inboxes.find(@config.fetch('inbox_id'))
    check(@inbox.channel_type == 'Channel::Api' && @inbox.channel.instance_of?(Channel::Api) &&
          @inbox.name.start_with?(PREFIX) && @inbox.name.length > PREFIX.length, 'inbox_identity')
    check(@account.inboxes.where(name: @inbox.name).pluck(:id) == [@inbox.id], 'inbox_name_not_unique')
    [@inbox, @inbox.channel].each do |record|
      check(record.attributes.none? { |key, value| key.match?(/(?:webhook|callback).*url/) && value.present? }, 'external_callback')
      check(!record.respond_to?(:callback_webhook_url) || record.callback_webhook_url.blank?, 'external_callback')
    end
    seed = @account.conversations.find_by(display_id: @config.fetch('seed_conversation_id'), inbox_id: @inbox.id)
    check(seed && seed.custom_attributes['acceptance_run_id'] == @config.fetch('seed_run_id'), 'seed_identity')
    @bot = AgentBot.find(@config.fetch('bot_id'))
    check(@bot.account_id == @account.id &&
          (seed.ai_assignee == @bot || seed.messages.where(message_type: :outgoing, sender_type: 'AgentBot', sender_id: @bot.id).exists?),
          'bot_seed_identity')
    check(defined?(NevidimyAgentBotConversationProjection) &&
          AgentBotListener < NevidimyAgentBotConversationProjection::Listener &&
          NevidimyAgentBotConversationProjection.internal_recipient?(@bot) &&
          ENV[NevidimyAgentBotConversationProjection::OPT_IN] == '1', 'internal_projection_identity')
    check(!AgentBotInbox.where(inbox_id: @inbox.id).exists?, 'existing_inbox_binding')
    check(!@inbox.enable_auto_assignment? && !@inbox.greeting_enabled? && !@inbox.working_hours_enabled?, 'inbox_side_effects')
    check(!CaptainInbox.exists?(inbox_id: @inbox.id), 'captain_inbox')
    check(!Integrations::Hook.enabled.where(account_id: @account.id).exists? &&
          !Webhook.where(account_id: @account.id).exists?, 'account_external_hooks')
    check(!@account.feature_enabled?('unread_count_for_filters') && !ChatwootApp.advanced_search_allowed?, 'account_external_writes')
    validate_sync_listeners(Rails.configuration.dispatcher.sync_dispatcher)
    # Pubsubable lazily regenerates missing user tokens; refuse that existing-row write.
    recipient_ids = (@inbox.members.pluck(:id) + @account.administrators.pluck(:id)).uniq
    check(!User.where(id: recipient_ids, pubsub_token: [nil, '']).exists?, 'recipient_token_backfill')
    check(!Contact.where(account_id: @account.id, identifier: @tag).exists? &&
          !Conversation.where('custom_attributes ->> ? = ?', 'yjit_probe_run_id', @config.fetch('run_id')).exists?, 'run_already_exists')
  end

  def core_models
    [Account, Inbox, Channel::Api, AgentBot, AgentBotInbox, User, AccountUser,
     Integrations::Hook, Webhook, Contact, ContactInbox, Conversation, Message]
  end

  def row_digest(model, fence)
    # Technical MVCC metadata only: never read text, custom attributes, tokens or dialogue.
    rows = model.unscoped.where('id <= ?', fence).reorder(:id).pluck(:id, Arel.sql('xmin::text'))
    Digest::SHA256.hexdigest(JSON.generate(rows))
  end

  def prepare_contact
    @contact = Contact.create!(account: @account, name: 'Neutral JIT benchmark', identifier: @tag,
                               contact_type: :lead, custom_attributes: { 'yjit_probe_run_id' => @config.fetch('run_id') })
    @created[Contact] << @contact.id
    @contact_inbox = ContactInbox.create!(contact: @contact, inbox: @inbox, source_id: @tag)
    @created[ContactInbox] << @contact_inbox.id
    check_jobs
  end

  def pair(index)
    @adapter.enqueued_jobs.clear
    check_jobs
    Current.reset
    conversation = Conversation.create!(account: @account, inbox: @inbox, contact: @contact,
                                        contact_inbox: @contact_inbox, status: :pending, ai_assignee: @bot,
                                        custom_attributes: { 'yjit_probe_run_id' => @config.fetch('run_id'),
                                                             'yjit_probe_pair' => index, 'reply_owner' => 'bot',
                                                             'ownership_version' => 3, 'context_epoch' => 0 })
    @created[Conversation] << conversation.id
    # Exercise an incoming waiting_since update and the bot's outgoing clear;
    # only this new, tagged row is touched. Do not alter native anti-flood limits.
    conversation.update_columns(waiting_since: nil)
    @adapter.enqueued_jobs.clear
    input, incoming = operation(conversation.id, 'incoming', index)
    output, outgoing = operation(conversation.id, 'outgoing', index, input.id)
    check(input.incoming? && input.sender == @contact && output.outgoing? && output.sender == @bot, 'native_sender')
    check(input.conversation_id == conversation.id && output.conversation_id == conversation.id &&
          input.inbox_id == @inbox.id && output.inbox_id == @inbox.id, 'native_message_scope')
    jobs = check_jobs
    check(jobs.fetch('SendReplyJob', 0) == 2 && jobs.fetch('EventDispatcherJob', 0).positive? &&
          jobs.fetch('ActionCableBroadcastJob', 0).positive?, 'native_callbacks_missing')
    @adapter.enqueued_jobs.clear
    { incoming: incoming, outgoing: outgoing, enqueued_jobs: jobs }
  end

  def operation(conversation_id, kind, index, input_id = nil)
    Current.reset
    Current.account, Current.inbox = @account, @inbox
    Current.user = @bot if kind == 'outgoing'
    conversation = @account.conversations.find(conversation_id)
    attributes = input_id ? { bot_turn_key: "message:#{input_id}" } : {}
    params = ActionController::Parameters.new(content: "Neutral JIT benchmark #{kind}", content_type: 'text',
                                              message_type: kind, private: false, content_attributes: attributes,
                                              source_id: "#{@tag}-#{index}-#{kind}")
    ActiveRecord::Base.connection.clear_query_cache
    message, sample = ActiveRecord::Base.cache do
      metrics { Messages::MessageBuilder.new(Current.user, conversation, params).perform }
    end
    @created[Message] << message.id
    [message, sample]
  end

  def metrics
    sql = { schema: 0, cached: 0, uncached: 0, schema_ms: 0.0, cached_ms: 0.0, uncached_ms: 0.0 }
    subscriber = ActiveSupport::Notifications.monotonic_subscribe('sql.active_record') do |_, started, finished, _, payload|
      kind = payload[:name] == 'SCHEMA' ? :schema : (payload[:cached] ? :cached : :uncached)
      sql[kind] += 1
      sql[:"#{kind}_ms"] += (finished - started) * 1000
    end
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    thread_cpu = Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID)
    process_cpu = Process.clock_gettime(Process::CLOCK_PROCESS_CPUTIME_ID)
    allocated = GC.stat(:total_allocated_objects)
    gc_time = GC.stat.fetch(:time, 0)
    result = yield
    sample = {
      elapsed_ms: ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000).round(3),
      thread_cpu_ms: ((Process.clock_gettime(Process::CLOCK_THREAD_CPUTIME_ID) - thread_cpu) * 1000).round(3),
      process_cpu_ms: ((Process.clock_gettime(Process::CLOCK_PROCESS_CPUTIME_ID) - process_cpu) * 1000).round(3),
      allocated_objects: GC.stat(:total_allocated_objects) - allocated,
      process_gc_ms: GC.stat.fetch(:time, 0) - gc_time,
      sql: sql.transform_values { |value| value.is_a?(Float) ? value.round(3) : value }
    }
    [result, sample]
  ensure
    ActiveSupport::Notifications.unsubscribe(subscriber) if subscriber
  end

  def check_jobs
    check(@adapter.perform_enqueued_jobs == false && @adapter.perform_enqueued_at_jobs == false &&
          @adapter.performed_jobs.empty?, 'automatic_job_execution')
    check(@adapter.enqueued_jobs.length <= 128, 'job_capture_unbounded')
    @adapter.enqueued_jobs.each_with_object(Hash.new(0)) { |job, counts| counts[job.fetch(:job).name] += 1 }
  end

  def verify_records
    @before_rows.each do |model, before|
      check(row_digest(model, before.fetch(:fence)) == before.fetch(:digest), 'preexisting_core_row_changed')
      new_ids = model.unscoped.where('id > ?', before.fetch(:fence)).reorder(:id).pluck(:id)
      check(new_ids == @created[model].sort, 'unexpected_core_record_created')
    end
    check(@created[Conversation].length == WARMUP_PAIRS + SAMPLE_PAIRS &&
          @created[Message].length == 2 * (WARMUP_PAIRS + SAMPLE_PAIRS), 'created_record_count')
    check(Conversation.where(id: @created[Conversation], contact_id: @contact.id, inbox_id: @inbox.id)
                      .where('custom_attributes ->> ? = ?', 'yjit_probe_run_id', @config.fetch('run_id')).count == @created[Conversation].length,
          'created_conversation_tag')
    check(Message.where(id: @created[Message], conversation_id: @created[Conversation]).count == @created[Message].length, 'created_message_scope')
  end

  def numeric_yjit_stats(stats)
    return {} unless stats.is_a?(Hash)

    stats.select { |key, value| YJIT_STATS.include?(key) && value.is_a?(Numeric) && value.finite? }
  end

  def yjit_stats
    numeric_yjit_stats(RubyVM::YJIT.runtime_stats)
  end

  def memory_from_status(status)
    { rss_kib: Integer(status[/^VmRSS:\s+(\d+) kB$/, 1]),
      peak_rss_kib: Integer(status[/^VmHWM:\s+(\d+) kB$/, 1]) }
  end

  def memory
    memory_from_status(File.read('/proc/self/status'))
  end
end

NevidimyNativeYjitProbe.new.run if defined?(Rails.application)

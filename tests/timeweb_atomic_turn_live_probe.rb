# One-off native Rails/Rack/DB verification, never a latency or delivery probe.
# The lead prepares the isolated API inbox, conversation and >= 6 incoming IDs.
# Runtime-only inputs: NEVIDIMY_PROBE_CONFIG, CHATWOOT_READ_TOKEN,
# CHATWOOT_BOT_TOKEN. Never invoke this against a customer conversation.
require 'json'
require 'logger'
require 'rack/mock'
require 'timeout'
require 'uri'
require 'active_job/queue_adapters/test_adapter'

class NevidimyAtomicTurnLiveProbe
  class Failure < StandardError; end

  INITIALIZER = '/run/nevidimy-check/atomic_turn.rb'.freeze
  PREFIX = 'Nevidimy atomic turn verification '.freeze
  SQL_COUNTER = :nevidimy_atomic_probe_sql_count

  def initialize
    @result = { ok: false, requests: [], checks: [], created: { message_ids: [] }, cleanup: {} }
    @workers = []
    @releases = []
    @patched_methods = []
    @adapters = {}
    @loggers = {}
  end

  def check(condition, code)
    raise Failure, code unless condition
  end

  def run
    silence_and_capture_jobs
    configure
    validate_target
    require INITIALIZER
    timing = '/run/nevidimy-check/turn_timing.rb'
    require timing if File.file?(timing)
    Rails.application.reloader.prepare!
    if defined?(NevidimyTurnTiming)
      ancestors = Api::V1::Accounts::Conversations::MessagesController.ancestors
      check(ancestors.index(NevidimyTurnTiming::Controller) < ancestors.index(NevidimyAtomicTurn::Commit), 'timing_scope_order')
    end
    check(Message._create_callbacks.any? { |callback| callback.filter == :nevidimy_lock_staff_conversation }, 'staff_callback_missing')
    check(ActiveRecord::Base.connection_pool.size >= 3, 'connection_pool_too_small')
    @subscriber = ActiveSupport::Notifications.subscribe('sql.active_record') do
      count = Thread.current.thread_variable_get(SQL_COUNTER)
      Thread.current.thread_variable_set(SQL_COUNTER, count + 1) unless count.nil?
    end
    prepare_conversation
    @binding = AgentBotInbox.new(inbox_id: @inbox.id, agent_bot_id: @bot.id)
    @binding.save!
    @result[:created][:binding_id] = @binding.id
    verify_commit_and_duplicate
    verify_rejections_and_rollback
    verify_assignment_status_and_epoch
    verify_concurrent_commit
    verify_staff_lock_race
    if defined?(NevidimyTurnTiming)
      check(@timing_events.length == 11, 'timing_event_count')
      @result[:timing_events] = @timing_events
      @result[:checks] << 'native_atomic_timing_scope_and_content_free_events'
    end
    @result[:ok] = true
  rescue Failure => error
    @result[:error] = error.message
  rescue StandardError => error
    @result[:error] = "unexpected_#{error.class.name}"
  ensure
    cleanup
    STDOUT.write(JSON.generate(@result) + "\n")
  end

  private

  def silence_and_capture_jobs
    quiet = Logger.new(File::NULL)
    events = @timing_events = []
    quiet.define_singleton_method(:info) do |message = nil, &block|
      if message.is_a?(String) && message.start_with?('{"event":"nevidimy_atomic_turn_timing"')
        event = JSON.parse(message)
        allowed = %w[event elapsed_ms spans sql outgoing_message_id http_status]
        raise NevidimyAtomicTurnLiveProbe::Failure, 'timing_extra_fields' unless (event.keys - allowed).empty?
        events << event
      end
      super(message, &block)
    end
    [Rails, ActiveRecord::Base, ActionController::Base, ActiveJob::Base].each do |owner|
      @loggers[owner] = owner.logger
      owner.logger = quiet
    end
    @job_adapter = ActiveJob::QueueAdapters::TestAdapter.new
    @job_adapter.perform_enqueued_jobs = false
    @job_adapter.perform_enqueued_at_jobs = false
    ([ActiveJob::Base] + ActiveJob::Base.descendants).uniq.each do |klass|
      @adapters[klass] = klass.queue_adapter
      klass.queue_adapter = @job_adapter
    end
  end

  def configure
    @config = JSON.parse(ENV.fetch('NEVIDIMY_PROBE_CONFIG'))
    keys = %w[account_id inbox_id conversation_id input_ids acceptance_run_id initializer_path]
    check(@config.is_a?(Hash) && @config.keys.sort == keys.sort, 'config_shape')
    %w[account_id inbox_id conversation_id].each do |key|
      check(@config[key].is_a?(Integer) && @config[key].positive?, 'config_id')
    end
    @inputs = @config['input_ids']
    check(@inputs.is_a?(Array) && @inputs.length >= 6 && @inputs.uniq.length == @inputs.length &&
          @inputs.all? { |id| id.is_a?(Integer) && id.positive? }, 'config_inputs')
    @inputs = @inputs.sort.first(6)
    check(@config['acceptance_run_id'].is_a?(String) &&
          @config['acceptance_run_id'].match?(/\A[a-zA-Z0-9_-]{8,128}\z/), 'config_run')
    check(@config['initializer_path'] == INITIALIZER && File.file?(INITIALIZER) &&
          File.realpath(INITIALIZER) == INITIALIZER, 'initializer_path')
    @read_token = ENV.fetch('CHATWOOT_READ_TOKEN')
    @bot_token = ENV.fetch('CHATWOOT_BOT_TOKEN')
    check(!@read_token.empty? && !@bot_token.empty?, 'tokens_missing')
    frontend = URI.parse(ENV.fetch('FRONTEND_URL', 'https://localhost'))
    check(%w[http https].include?(frontend.scheme) && frontend.host.present? &&
          frontend.userinfo.nil? && frontend.query.nil? && frontend.fragment.nil?, 'frontend_host')
    @rack_origin = URI::HTTPS.build(host: frontend.host,
                                   port: frontend.port == frontend.default_port ? nil : frontend.port).to_s
  rescue JSON::ParserError, KeyError, URI::InvalidURIError
    raise Failure, 'config_or_token_missing'
  end

  def validate_target
    @account = Account.find_by(id: @config['account_id'])
    check(!@account.nil?, 'account_missing')
    @inbox = @account.inboxes.find_by(id: @config['inbox_id'])
    check(@inbox && @inbox.channel_type == 'Channel::Api' && @inbox.name.start_with?(PREFIX) &&
          @inbox.name.length > PREFIX.length, 'inbox_identity')
    check(@account.inboxes.where(name: @inbox.name).pluck(:id) == [@inbox.id], 'inbox_name_not_unique')
    [@inbox, @inbox.channel].each do |record|
      check(!record.nil?, 'api_channel_missing')
      urls = record.attributes.select { |key, _| key.match?(/(?:webhook|callback).*url/) }.values
      %i[webhook_url callback_webhook_url].each do |method|
        urls << record.public_send(method) if record.respond_to?(method)
      end
      check(urls.all?(&:blank?), 'inbox_callback_present')
    end
    @conversation = @account.conversations.find_by(display_id: @config['conversation_id'], inbox_id: @inbox.id)
    check(@conversation && @conversation.custom_attributes.is_a?(Hash) &&
          @conversation.custom_attributes['acceptance_run_id'] == @config['acceptance_run_id'], 'conversation_identity')
    rows = @conversation.messages.where(id: @config['input_ids']).pluck(:id, :message_type, :private)
    check(rows.length == @config['input_ids'].length &&
          rows.all? { |_, kind, private_message| (kind == 'incoming' || kind == 0) && private_message == false }, 'input_identity')
    check(!AgentBotInbox.where(inbox_id: @inbox.id).exists?, 'inbox_already_bound')
    @bot = AccessToken.find_by(token: @bot_token)&.owner
    @staff = AccessToken.find_by(token: @read_token)&.owner
    check(@bot.is_a?(AgentBot) && @bot.account_id == @account.id, 'bot_account_scope')
    check(@staff.is_a?(User) && AccountUser.exists?(account_id: @account.id, user_id: @staff.id), 'read_account_scope')
    @safe_target = true
    @result[:target] = { account_id: @account.id, inbox_id: @inbox.id,
                         conversation_id: @conversation.display_id, input_ids: @inputs }
  end

  def prepare_conversation
    check(!@conversation.messages.where(message_type: :outgoing).exists?, 'conversation_not_fresh')
    @conversation.update!(status: :pending, assignee_id: nil, team_id: nil,
                          custom_attributes: @conversation.custom_attributes.merge(
                            'reply_owner' => 'bot', 'ownership_version' => 3, 'context_epoch' => 0,
                            'workflow_state' => 'open_conversation', 'scenario' => {},
                            'scenario_pending_input' => nil, 'scenario_last_message_id' => 0,
                            'scenario_followups' => {}
                          ))
    @prepared = true
  end

  def request(label, method, path, token, payload = nil)
    env = Rack::MockRequest.env_for("#{@rack_origin}#{path}", method: method,
                                   input: payload ? JSON.generate(payload) : '',
                                   'CONTENT_TYPE' => 'application/json', 'HTTP_API_ACCESS_TOKEN' => token,
                                   'HTTPS' => 'on', 'rack.url_scheme' => 'https')
    Thread.current.thread_variable_set(SQL_COUNTER, 0)
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    status, _, body = Rails.application.call(env)
    raw = +''
    body.each { |part| raw << part }
    data = begin
      raw.empty? ? {} : JSON.parse(raw)
    rescue JSON::ParserError
      {}
    end
    data = {} unless data.is_a?(Hash)
    row = { check: label, status: status, sql: Thread.current.thread_variable_get(SQL_COUNTER),
            duration_ms: ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000).round(3) }
    row[:outcome] = data['outcome'] if %w[committed duplicate].include?(data['outcome'])
    row[:id] = data['id'] if data['id'].is_a?(Integer)
    @result[:requests] << row
    { status: status, data: data }
  ensure
    body.close if body.respond_to?(:close)
    Thread.current.thread_variable_set(SQL_COUNTER, nil)
    Current.reset
  end

  def path
    "/api/v1/accounts/#{@account.id}/conversations/#{@conversation.display_id}"
  end

  def snapshot(id, label)
    response = request(label, 'GET', "#{path}?nevidimy_turn_state=#{id}", @read_token)
    data = response[:data]
    check(response[:status] == 200 && data['capability'] == 'nevidimy-atomic-turn-v1' &&
          data['input_id'] == id && data['eligible'] == true, 'snapshot_ineligible')
    data
  end

  def payload(snapshot, id)
    { content: 'Neutral atomic verification reply', content_type: 'text',
      content_attributes: { bot_turn_key: "message:#{id}" },
      nevidimy_atomic_turn: { input_id: id, version: snapshot.fetch('version'),
                             attributes: { workflow_state: 'open_conversation',
                                           scenario_pending_input: nil, scenario_last_message_id: id } } }
  end

  def post(label, body)
    request(label, 'POST', "#{path}/messages", @bot_token, body)
  end

  def state
    @conversation.reload
    { attributes: @conversation.custom_attributes.deep_dup, count: @conversation.messages.count,
      version: @conversation.updated_at }
  end

  def replies(id)
    rows = @conversation.messages.where(message_type: :outgoing, private: false)
                        .where('id > ?', id).select(:id, :content_attributes)
    ids = rows.filter_map do |message|
      attributes = message.content_attributes
      message.id if attributes.is_a?(Hash) && attributes['bot_turn_key'] == "message:#{id}"
    end
    @conversation.messages.where(id: ids)
  end

  def sends
    @job_adapter.enqueued_jobs.count { |job| job[:job].name == 'SendReplyJob' }
  end

  def check_reply(response, outcome, id)
    message_id = response[:data]['id']
    @result[:created][:message_ids] |= [message_id] if message_id.is_a?(Integer)
    valid = response[:status] == 200 && response[:data]['outcome'] == outcome &&
            message_id.is_a?(Integer) && replies(id).count == 1
    diagnose_reply(id, message_id) unless valid
    check(valid, "#{outcome}_response")
    matching = replies(id).pick(:id) == message_id
    diagnose_reply(id, message_id) unless matching
    check(matching, 'reply_identity')
  end

  def diagnose_reply(input_id, message_id)
    # Failure-only structural metadata. Stay inside the already validated
    # synthetic conversation; never output column values, message content,
    # SQL text/binds, tokens, connection addresses or database/user names.
    outgoing = @conversation.messages.where(message_type: :outgoing, private: false)
    expected = "message:#{input_id}"
    metadata = {
      input_id: input_id,
      outgoing_enum_count: outgoing.count,
      decoded_relation_count: replies(input_id).count,
      decoded_relation_uncached_count: Message.uncached { replies(input_id).count },
      decoded_match_count: Message.uncached do
        outgoing.select(:id, :content_attributes).to_a.count do |message|
          attributes = message.content_attributes
          attributes.is_a?(Hash) && attributes['bot_turn_key'] == expected
        end
      end
    }
    if message_id.is_a?(Integer)
      metadata[:message_id] = message_id
      message = Message.uncached { @conversation.messages.find_by(id: message_id) }
      metadata[:message_in_conversation] = !message.nil?
      if message
        attributes = message.content_attributes
        metadata[:message_outgoing] = message.outgoing?
        metadata[:message_public] = message.private? == false
        metadata[:decoded_type] = attributes.is_a?(Hash) ? 'Hash' : attributes.class.name
        metadata[:decoded_key_matches] = attributes.is_a?(Hash) && attributes['bot_turn_key'] == expected
        raw = ActiveRecord::Base.connection.raw_connection.exec_params(
          'SELECT pg_typeof(content_attributes)::text AS column_type, ' \
          'json_typeof(content_attributes::json) AS json_kind ' \
          'FROM messages WHERE conversation_id = $1 AND id = $2',
          [@conversation.id, message_id]
        )
        if raw.ntuples == 1
          metadata[:column_type] = raw.getvalue(0, 0)
          metadata[:raw_json_kind] = raw.getvalue(0, 1)
        end
      end
    end
    raw_counts = ActiveRecord::Base.connection.raw_connection.exec_params(
      'SELECT COUNT(*) FILTER (WHERE message_type = $2 AND private = FALSE), ' \
      'COUNT(*) FILTER (WHERE message_type = $2 AND private = FALSE ' \
      "AND content_attributes ->> 'bot_turn_key' = $3) FROM messages WHERE conversation_id = $1",
      [@conversation.id, Message.message_types.fetch('outgoing'), expected]
    )
    metadata[:raw_outgoing_enum_count] = raw_counts.getvalue(0, 0).to_i
    metadata[:raw_turn_key_count] = raw_counts.getvalue(0, 1).to_i
    role = ActiveRecord::Base.current_role.to_s
    metadata[:connection_role] = %w[writing reading].include?(role) ? role : 'other'
    metadata[:transaction_open] = ActiveRecord::Base.connection.transaction_open?
    recovery = ActiveRecord::Base.connection.raw_connection.exec(
      "SELECT pg_is_in_recovery(), current_setting('transaction_read_only') = 'on'"
    )
    metadata[:replica_recovery] = [true, 't'].include?(recovery.getvalue(0, 0))
    metadata[:transaction_read_only] = [true, 't'].include?(recovery.getvalue(0, 1))
    (@result[:diagnostics] ||= []) << metadata
  rescue StandardError => error
    metadata ||= { input_id: input_id }
    metadata[:diagnostic_error] = error.class.name
    (@result[:diagnostics] ||= []) << metadata
  end

  def verify_commit_and_duplicate
    id = @inputs[0]
    body = payload(snapshot(id, 'commit_read'), id)
    jobs = sends
    first = post('commit', body)
    check_reply(first, 'committed', id)
    check(sends == jobs + 1, 'commit_send_job_count')
    @conversation.reload.update!(custom_attributes: @conversation.custom_attributes.merge('workflow_need' => 'probe_concurrent'))
    before = state
    jobs = sends
    queued = @job_adapter.enqueued_jobs.length
    duplicate = post('duplicate', body)
    check_reply(duplicate, 'duplicate', id)
    check(duplicate[:data]['id'] == first[:data]['id'] && state == before && sends == jobs &&
          @job_adapter.enqueued_jobs.length == queued, 'duplicate_changed_state')
    @result[:checks] << 'commit_duplicate_no_overwrite'
  end

  def verify_rejections_and_rollback
    id = @inputs[1]
    body = payload(snapshot(id, 'invalid_read'), id)
    invalid = body.deep_dup
    invalid[:nevidimy_atomic_turn][:attributes][:unknown_probe_field] = true
    before, queued = state, @job_adapter.enqueued_jobs.length
    check(post('invalid', invalid)[:status] == 409 && state == before &&
          @job_adapter.enqueued_jobs.length == queued, 'invalid_partial_write')
    body = payload(snapshot(id, 'conflict_read'), id)
    @conversation.reload.update!(custom_attributes: @conversation.custom_attributes.merge('workflow_need' => 'probe_changed'))
    before, queued = state, @job_adapter.enqueued_jobs.length
    check(post('conflict', body)[:status] == 409 && state == before &&
          @job_adapter.enqueued_jobs.length == queued, 'conflict_partial_write')
    body = payload(snapshot(id, 'rollback_read'), id)
    patch = Module.new do
      def perform
        # ActiveRecord swallows this only at the enclosing transaction boundary:
        # verify a real rollback without invoking production error reporters.
        raise ActiveRecord::Rollback, 'probe_builder_failure' if Thread.current.thread_variable_get(:nevidimy_probe_builder_failure)
        super
      end
    end
    Messages::MessageBuilder.prepend(patch)
    @patched_methods << [patch, :perform]
    before, queued = state, @job_adapter.enqueued_jobs.length
    Thread.current.thread_variable_set(:nevidimy_probe_builder_failure, true)
    response = post('rollback', body)
    check(response[:status] >= 400 && state == before &&
          @job_adapter.enqueued_jobs.length == queued && !replies(id).exists?, 'rollback_partial_write')
    @result[:checks] << 'invalid_conflict_rollback_no_writes'
  ensure
    Thread.current.thread_variable_set(:nevidimy_probe_builder_failure, false)
  end

  def verify_assignment_status_and_epoch
    id = @inputs[2]
    body = payload(snapshot(id, 'assignment_read'), id)
    # Preserve the timestamp deliberately: test fresh native ownership/status,
    # not just the independent optimistic-version check.
    @conversation.update_columns(assignee_id: @staff.id)
    before, jobs = state, sends
    check(post('assignment_race', body)[:status] == 409 && state == before && sends == jobs, 'assignment_race_write')
    @conversation.update_columns(assignee_id: nil, status: Conversation.statuses.fetch('pending'))
    body = payload(snapshot(id, 'status_read'), id)
    @conversation.update_columns(status: Conversation.statuses.fetch('resolved'))
    before, jobs = state, sends
    check(post('status_race', body)[:status] == 409 && state == before && sends == jobs, 'status_race_write')
    @conversation.update_columns(status: Conversation.statuses.fetch('pending'))
    id = @inputs[3]
    body = payload(snapshot(id, 'epoch_read'), id)
    body[:nevidimy_atomic_turn][:attributes][:context_epoch] = @conversation.reload.custom_attributes['context_epoch']
    @conversation.update_columns(custom_attributes: @conversation.custom_attributes.merge('context_epoch' => 1))
    before, jobs = state, sends
    check(post('epoch_race', body)[:status] == 409 && state == before && sends == jobs, 'epoch_race_write')
    @result[:checks] << 'fresh_assignment_status_epoch_guards'
  end

  def worker(&block)
    thread = Thread.new do
      Thread.current.report_on_exception = false
      Rails.application.executor.wrap do
        ActiveRecord::Base.connection_pool.with_connection { block.call }
      end
    rescue Failure => error
      { error: error.message }
    rescue StandardError => error
      { error: "worker_#{error.class.name}" }
    ensure
      Current.reset
    end
    @workers << thread
    thread
  end

  def await(thread)
    check(thread.join(10), 'worker_timeout')
    value = thread.value
    check(!value.is_a?(Hash) || !value.key?(:error), value.is_a?(Hash) ? value[:error].to_s : 'worker_error')
    value
  end

  def verify_concurrent_commit
    id = @inputs[4]
    body = payload(snapshot(id, 'concurrent_read'), id)
    ready, release = Queue.new, Queue.new
    @releases << release
    jobs = sends
    threads = 2.times.map do |index|
      worker do
        ready << true
        release.pop
        post("same_turn_#{index}", body)
      end
    end
    Timeout.timeout(5) { 2.times { ready.pop } }
    2.times { release << true }
    results = threads.map { |thread| await(thread) }
    check(results.all? { |result| result[:status] == 200 } &&
          results.map { |result| result[:data]['outcome'] }.sort == %w[committed duplicate] &&
          results.map { |result| result[:data]['id'] }.uniq.length == 1 &&
          replies(id).count == 1 && sends == jobs + 1, 'concurrent_duplicate_write')
    @result[:created][:message_ids] |= replies(id).pluck(:id)
    @result[:checks] << 'two_real_commits_one_message_one_send_job'
  end

  def verify_staff_lock_race
    id = @inputs[5]
    body = payload(snapshot(id, 'staff_race_read'), id)
    entered, release, bot_pid = Queue.new, Queue.new, Queue.new
    @releases << release
    patch = Module.new do
      define_method(:nevidimy_lock_staff_conversation) do
        result = super()
        gate = Thread.current.thread_variable_get(:nevidimy_probe_staff_gate)
        if gate && outgoing? && !private? && sender_type == 'User'
          gate[0] << true
          gate[1].pop
        end
        result
      end
      private :nevidimy_lock_staff_conversation
    end
    Message.prepend(patch)
    @patched_methods << [patch, :nevidimy_lock_staff_conversation]
    lock_probe = Module.new do
      define_method(:lock!) do |*arguments, &block|
        observer = Thread.current.thread_variable_get(:nevidimy_probe_bot_lock)
        # Query inside with_lock's transaction so a managed transaction pooler
        # cannot substitute its protocol connection PID or another backend.
        observer << self.class.connection.select_value('SELECT pg_backend_pid()').to_i if observer
        super(*arguments, &block)
      end
    end
    Conversation.prepend(lock_probe)
    @patched_methods << [lock_probe, :lock!]
    before = state
    jobs = sends
    staff = worker do
      Current.user, Current.account = @staff, @account
      Thread.current.thread_variable_set(:nevidimy_probe_staff_gate, [entered, release])
      message = Message.create!(account_id: @account.id, inbox_id: @inbox.id,
                                conversation_id: @conversation.id, sender: @staff,
                                message_type: :outgoing, private: false, content: 'Neutral staff verification reply')
      { message_id: message.id }
    ensure
      Thread.current.thread_variable_set(:nevidimy_probe_staff_gate, nil)
    end
    @result[:lock_probe] = { staff_barrier: false, pid_captured: false, blocked: false }
    Timeout.timeout(5) { entered.pop }
    @result[:lock_probe][:staff_barrier] = true
    bot = worker do
      # Capture the actual connection immediately before the guarded row lock.
      # Rack/executor setup can recycle the worker's earlier connection lease.
      Thread.current.thread_variable_set(:nevidimy_probe_bot_lock, bot_pid)
      post('public_staff_race', body)
    ensure
      Thread.current.thread_variable_set(:nevidimy_probe_bot_lock, nil)
    end
    pid = Timeout.timeout(5) { bot_pid.pop }
    @result[:lock_probe][:pid_captured] = true
    locked = false
    Timeout.timeout(5) do
      until locked
        observed = ActiveRecord::Base.connection.raw_connection.exec_params(
          'SELECT cardinality(pg_blocking_pids($1)) > 0', [pid]
        ).getvalue(0, 0)
        @result[:lock_probe][:blocking_result_type] = observed.class.name
        # Rails may install PG result decoders on raw_connection as well.
        locked = observed == true || observed == 't'
        sleep 0.01 unless locked
      end
    end
    @result[:lock_probe][:blocked] = locked
    release << true
    staff_result, bot_result = await(staff), await(bot)
    @result[:created][:message_ids] |= [staff_result.fetch(:message_id)]
    after = state
    check(locked && bot_result[:status] == 409 && after[:attributes] == before[:attributes] &&
          after[:count] == before[:count] + 1 && !replies(id).exists? && sends == jobs + 1, 'public_staff_race_write')
    @result[:checks] << 'real_staff_before_create_lock_blocks_bot_then_conflicts'
  end

  def cleanup
    @releases.each { |queue| 4.times { queue << true } }
    @workers.each do |thread|
      next if thread.join(2)
      thread.kill
      thread.join
    end
    @patched_methods.reverse_each { |mod, method| mod.send(:remove_method, method) }
    if @binding && @binding.id
      @result[:created][:binding_id] = @binding.id
      begin
        binding = AgentBotInbox.find_by(id: @binding.id, inbox_id: @inbox.id, agent_bot_id: @bot.id)
        binding.destroy! if binding
        @result[:cleanup][:binding_removed] = !AgentBotInbox.exists?(id: @binding.id)
        check(@result[:cleanup][:binding_removed], 'binding_cleanup_incomplete')
      rescue StandardError => error
        @result[:ok] = false
        @result[:cleanup][:binding_error] = "binding_cleanup_#{error.class.name}"
      end
    end
    if @safe_target
      begin
        @conversation.reload
        check(@conversation.inbox_id == @inbox.id &&
              @conversation.custom_attributes['acceptance_run_id'] == @config['acceptance_run_id'], 'cleanup_identity')
        if @prepared
          @result[:created][:message_ids] |= @conversation.messages.where(message_type: :outgoing).pluck(:id)
        end
        @conversation.update!(status: :resolved, assignee_id: nil)
        @result[:cleanup][:conversation_resolved] = @conversation.reload.resolved?
        check(@result[:cleanup][:conversation_resolved], 'conversation_cleanup_incomplete')
      rescue StandardError => error
        @result[:ok] = false
        @result[:cleanup][:conversation_error] = error.is_a?(Failure) ? error.message : "conversation_cleanup_#{error.class.name}"
      end
    end
    @result[:jobs] = @job_adapter.enqueued_jobs.each_with_object(Hash.new(0)) do |job, counts|
      counts[job[:job].name] += 1
    end if @job_adapter
  rescue StandardError => error
    @result[:ok] = false
    @result[:cleanup][:error] = error.is_a?(Failure) ? error.message : "cleanup_#{error.class.name}"
  ensure
    ActiveSupport::Notifications.unsubscribe(@subscriber) if @subscriber
    @adapters.each { |klass, adapter| klass.queue_adapter = adapter }
    @loggers.each { |owner, logger| owner.logger = logger }
  end
end

probe = NevidimyAtomicTurnLiveProbe.new
probe.run
exit(probe.instance_variable_get(:@result)[:ok] ? 0 : 1)

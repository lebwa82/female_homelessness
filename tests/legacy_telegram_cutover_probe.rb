# Real Rails/PostgreSQL probe. All fixtures are synthetic and rolled back.
# No Telegram API calls, delivery callbacks, or queued work are permitted.
ActiveJob::Base.before_enqueue { raise 'unexpected_cutover_job' }

def require_cutover(condition, name)
  raise "probe_failed_#{name}" unless condition
end

models = [Channel::Api, Channel::Telegram, Inbox, InboxMember, Contact,
          ContactInbox, Conversation, Message, AgentBotInbox]
before = models.map(&:count)
checks = []
ActiveRecord::Base.transaction do
  history = cfg.fetch('history').deep_dup
  history['source'] = "nevidimy-manual-telegram:probe:#{SecureRandom.hex(8)}"
  imported = LegacyHistoryImporter.new('account_id' => 1, 'history' => history, 'apply' => true).run
  inbox = Inbox.find(imported.fetch(:inbox_id))
  source_links = inbox.contact_inboxes.order(:id).pluck(:id, :contact_id, :source_id)
  source_conversations = inbox.conversations.order(:id).pluck(:id, :display_id, :contact_inbox_id)
  input = cfg.merge('history' => history, 'inbox_id' => inbox.id, 'apply' => false)
  baseline = models.map(&:count)

  report = LegacyTelegramCutover.new(input).run
  require_cutover(report[:bot_owned] == 2 && report[:messages] == 4 &&
                  inbox.reload.channel_type == 'Channel::Api' &&
                  models.map(&:count) == baseline, 'dry_run_rollback')
  checks << 'dry_run_rollback'

  tampered = input.deep_dup
  tampered['history']['users'][0]['messages'][0]['sha256'] = 'wrong'
  rejected = false
  begin
    LegacyTelegramCutover.new(tampered).run
  rescue => error
    rejected = error.message == 'history_fingerprint_mismatch'
  end
  require_cutover(rejected && models.map(&:count) == baseline &&
                  inbox.reload.channel_type == 'Channel::Api', 'tamper_rejected')
  checks << 'tamper_rejected'

  input['apply'] = true
  report = LegacyTelegramCutover.new(input).run
  inbox.reload
  require_cutover(report[:bot_owned] == 2 && !report[:historical_messages_replayed] &&
                  inbox.messages.count == 4 && inbox.channel_type == 'Channel::Telegram', 'promotion_without_replay')
  require_cutover(inbox.contact_inboxes.order(:id).pluck(:id, :contact_id, :source_id) == source_links &&
                  inbox.conversations.order(:id).pluck(:id, :display_id, :contact_inbox_id) == source_conversations,
                  'identities_preserved')
  require_cutover(inbox.agent_bot_inbox.agent_bot_id == cfg['agent_bot_id'] &&
                  inbox.agent_bot_inbox.active?, 'native_agent_binding')
  require_cutover(inbox.conversations.all? { |c| c.pending? && c.assignee_id.nil? &&
                  c.assignee_agent_bot_id.nil? && c.custom_attributes['workflow_state'] == 'open_conversation' &&
                  c.custom_attributes['scenario'] == {} && c.custom_attributes['context_epoch'] == 0 },
                  'history_available_to_bot')
  checks.concat(%w[promotion_without_replay identities_preserved native_agent_binding history_available_to_bot])

  # Subsequent operator ownership must survive an accidental rerun.
  conversation = inbox.conversations.order(:id).first
  conversation.update_columns(assignee_id: User.find(1).id,
                              custom_attributes: conversation.custom_attributes.merge('reply_owner' => 'human'))
  baseline = models.map(&:count)
  repeated = LegacyTelegramCutover.new(input).run
  require_cutover(repeated[:already_promoted] && repeated[:bot_owned] == 1 &&
                  conversation.reload.assignee_id == 1 && models.map(&:count) == baseline, 'idempotent_without_stealing')
  checks << 'idempotent_without_stealing'
  raise ActiveRecord::Rollback
end
require_cutover(models.map(&:count) == before, 'fully_rolled_back')
checks.concat(%w[fully_rolled_back no_jobs_enqueued])
puts 'ACTIVATION_RESULT=' + {probe_passed: true, checks: checks}.to_json

# Executed on real Rails/PostgreSQL in a rolled-back, synthetic transaction.
# No customer messages or bot/Telegram API calls. A queued job fails this probe.
class LegacyImportProbeSideEffect < StandardError; end
ActiveJob::Base.before_enqueue { raise LegacyImportProbeSideEffect }

def require_probe(condition, name)
  raise "probe_failed_#{name}" unless condition
end

models = [Channel::Api, Inbox, InboxMember, Contact, ContactInbox, Conversation, Message]
before = models.map(&:count)
checks = []
ActiveRecord::Base.transaction do
  history = cfg.fetch('history').deep_dup
  history['source'] = "nevidimy-manual-telegram:probe:#{SecureRandom.hex(8)}"
  input = {account_id: cfg.fetch('account_id'), history: history, apply: false}.stringify_keys
  importer = ->(data = input) { LegacyHistoryImporter.new(data).run }
  result = importer.call
  require_probe(models.map(&:count) == before && !result[:applied], 'dry_run_rollback')
  checks << 'dry_run_rollback'

  input['apply'] = true
  result = importer.call
  inbox = Inbox.find(result[:inbox_id])
  require_probe(inbox.conversations.count == 2 && inbox.messages.count == 4, 'initial_counts')
  require_probe(inbox.conversations.where(status: :open).count == 1 &&
                inbox.conversations.where(status: :resolved).count == 1, 'open_and_closed')
  require_probe(!inbox.agent_bot_inbox && inbox.channel.webhook_url.blank?, 'no_external_channel')
  require_probe(inbox.inbox_members.count == Account.find(cfg.fetch('account_id')).account_users.count, 'team_visibility')
  require_probe(inbox.messages.where(content_type: :text).count == 4 &&
                inbox.messages.all? { |m| m.content_attributes == {} && m.source_id.nil? }, 'text_only')
  checks.concat(%w[initial_counts open_and_closed no_external_channel team_visibility text_only])

  repeat = importer.call
  require_probe(repeat[:counts][:messages_created] == 0 && repeat[:counts][:messages_verified] == 4, 'idempotent')
  checks << 'idempotent'
  broken = input.deep_dup
  broken['history']['users'][0]['messages'][0]['content'] = 'different text'
  rejected = false
  begin
    importer.call(broken)
  rescue => error
    rejected = error.message == 'stored_message_mismatch'
  end
  require_probe(rejected && inbox.messages.count == 4, 'tamper_rejected')
  checks << 'tamper_rejected'

  shorter = input.deep_dup
  shorter['history']['users'][0]['messages'].pop
  rejected = false
  begin
    importer.call(shorter)
  rescue => error
    rejected = error.message == 'older_or_incomplete_snapshot'
  end
  require_probe(rejected && inbox.messages.count == 4, 'older_snapshot_rejected')
  checks << 'older_snapshot_rejected'

  delta = input.deep_dup
  extra = delta['history']['users'][0]['messages'][0].deep_dup
  extra.merge!('id' => 5, 'content' => 'Incremental synthetic message', 'sha256' => 'probe-next')
  delta['history']['users'][0]['messages'] << extra
  operator_conversation = inbox.conversations.find_by!(status: :open)
  operator_conversation.update_columns(status: Conversation.statuses.fetch('resolved'))
  result = importer.call(delta)
  require_probe(result[:counts][:messages_created] == 1 && inbox.messages.count == 5 &&
                operator_conversation.reload.resolved?, 'append_without_operator_reset')
  checks << 'append_without_operator_reset'

  # Fail after making an early write: the whole import must roll back.
  broken = delta.deep_dup
  broken['history']['users'][0]['messages'] << extra.merge('id' => 6)
  broken['history']['users'][1]['messages'][0]['sha256'] = 'changed'
  rejected = false
  begin
    importer.call(broken)
  rescue => error
    rejected = error.message == 'message_fingerprint_conflict'
  end
  require_probe(rejected && inbox.messages.count == 5, 'atomic_failure')
  checks << 'atomic_failure'
  raise ActiveRecord::Rollback
end
require_probe(models.map(&:count) == before, 'probe_fully_rolled_back')
checks.concat(%w[probe_fully_rolled_back no_jobs_enqueued])
puts 'ACTIVATION_RESULT=' + {probe_passed: true, checks: checks}.to_json

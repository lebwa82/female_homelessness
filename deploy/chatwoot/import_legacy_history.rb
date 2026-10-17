# Historical import only. No model callbacks, delivery jobs, bot events, or timers.
# Run via activate.rails; cfg contains parsed data, never executable source SQL.
require 'securerandom'
require 'time'

class LegacyHistoryImporter
  KEY = 'legacy_telegram_import'.freeze
  NAME = 'История ручного бота — Telegram ещё не подключён'.freeze

  def initialize(config)
    @history = config.fetch('history')
    raise 'unsupported_format' unless @history['format'] == 1
    @source = @history.fetch('source')
    raise 'unsupported_source' unless @source.match?(/\Anevidimy-manual-telegram(?::probe:[0-9a-f]{16})?\z/)
    @apply = config.fetch('apply', false)
    @account = Account.find(config.fetch('account_id'))
    @counts = Hash.new(0)
    @now = Time.current
  end

  def insert(model, attributes)
    # insert_all! intentionally skips after_commit: historical outgoing messages
    # must never reach Telegram and incoming ones must never invoke Agent Bot.
    id = model.insert_all!([attributes], returning: %w[id]).rows.fetch(0).fetch(0)
    model.find(id)
  end

  def run
    result = nil
    ActiveRecord::Base.transaction(requires_new: true) do
      # One migration at a time; normal Chatwoot traffic continues independently.
      ActiveRecord::Base.connection.execute('SELECT pg_advisory_xact_lock(1846172041)')
      @inbox = staging_inbox
      known_ids = @inbox.conversations.map { |c| c.custom_attributes.dig(KEY, 'user_id') }
      raise 'older_or_incomplete_snapshot' unless (known_ids - @history.fetch('users').map { |u| u.fetch('id') }).empty?
      @history.fetch('users').each { |user| import_user(user) }
      result = {applied: @apply, inbox_id: @inbox.id, counts: @counts,
                external_delivery: false, agent_bot_connected: false}
      raise ActiveRecord::Rollback unless @apply
    end
    result
  end

  def staging_inbox
    channels = Channel::Api.where(account_id: @account.id)
                          .where('additional_attributes @> ?', {KEY => @source}.to_json)
    raise 'ambiguous_import_channel' if channels.count > 1
    channel = channels.first
    if channel
      inbox = channel.inbox
      raise 'missing_import_inbox' unless inbox
      raise 'import_channel_is_live' if channel.webhook_url.present? || inbox.agent_bot_inbox
      raise 'unrelated_inbox_conversations' if inbox.conversations.any? do |c|
        c.custom_attributes.dig(KEY, 'source') != @source
      end
      return inbox
    end
    channel = insert(Channel::Api, account_id: @account.id, identifier: SecureRandom.hex(16),
                     hmac_token: SecureRandom.hex(32), secret: SecureRandom.hex(32),
                     hmac_mandatory: true, additional_attributes: {KEY => @source},
                     created_at: @now, updated_at: @now)
    inbox = insert(Inbox, account_id: @account.id, channel_id: channel.id,
                   channel_type: 'Channel::Api', name: NAME, enable_auto_assignment: false,
                   greeting_enabled: false, csat_survey_enabled: false,
                   lock_to_single_conversation: true, created_at: @now, updated_at: @now)
    @account.account_users.pluck(:user_id).each do |user_id|
      insert(InboxMember, user_id: user_id, inbox_id: inbox.id, created_at: @now, updated_at: @now)
    end
    @counts[:inboxes_created] += 1
    inbox
  end

  def import_user(user)
    created = Time.iso8601(user.fetch('created_at'))
    updated = Time.iso8601(user.fetch('updated_at'))
    source_id = user.fetch('telegram_id')
    identity = "#{@source}:#{user.fetch('id')}"
    contact_inbox = ContactInbox.find_by(inbox_id: @inbox.id, source_id: source_id)
    metadata = {source: @source, user_id: user.fetch('id'),
                legacy_state: user.fetch('legacy_state'), unfinished: !user.fetch('finished')}
    if contact_inbox
      raise 'contact_identity_conflict' unless contact_inbox.contact.identifier == identity
      conversation = contact_inbox.conversations.sole
      raise 'conversation_identity_conflict' unless conversation.custom_attributes.dig(KEY, 'user_id') == user.fetch('id')
      @counts[:users_existing] += 1
    else
      raise 'contact_identity_conflict' if @account.contacts.exists?(identifier: identity)
      contact = insert(Contact, account_id: @account.id, identifier: identity,
                       name: "Клиентка ручного бота №#{user.fetch('id')}",
                       additional_attributes: {social_telegram_user_id: source_id},
                       custom_attributes: {KEY => metadata}, created_at: created, updated_at: updated)
      contact_inbox = insert(ContactInbox, contact_id: contact.id, inbox_id: @inbox.id,
                             source_id: source_id, pubsub_token: SecureRandom.hex(32),
                             hmac_verified: false, created_at: created, updated_at: updated)
      conversation = insert(Conversation, account_id: @account.id, inbox_id: @inbox.id,
                            contact_id: contact.id, contact_inbox_id: contact_inbox.id,
                            status: user.fetch('finished') ? 'resolved' : 'open',
                            uuid: SecureRandom.uuid, additional_attributes: {chat_id: source_id},
                            custom_attributes: {KEY => metadata},
                            created_at: created, updated_at: updated, last_activity_at: updated)
      @counts[:users_created] += 1
    end
    raise 'invalid_conversation_display_id' unless conversation.display_id&.positive?
    import_messages(user, conversation, contact_inbox.contact_id)
    # Never overwrite an operator's decisions on subsequent incremental imports.
    last_time = conversation.messages.maximum(:created_at) || updated
    conversation.update_columns(last_activity_at: last_time, updated_at: [last_time, updated].max)
    contact_inbox.contact.update_columns(last_activity_at: last_time)
    @counts[:users_verified] += 1
  end

  def import_messages(user, conversation, contact_id)
    imported = conversation.messages.select { |m| m.additional_attributes[KEY] }
    existing = imported.to_h do |message|
      info = message.additional_attributes.fetch(KEY)
      raise 'message_source_conflict' unless info['source'] == @source && info['message_id'].is_a?(Integer)
      [info['message_id'], message]
    end
    raise 'duplicate_imported_message' unless existing.size == imported.size
    # An incremental export may append data, but may never omit or rewrite imports.
    incoming_ids = user.fetch('messages').map { |m| m.fetch('id') }
    raise 'older_or_incomplete_snapshot' unless (existing.keys.compact - incoming_ids).empty?
    user.fetch('messages').each do |source|
      row = {account_id: @account.id, inbox_id: @inbox.id, conversation_id: conversation.id,
             content: source.fetch('content'), processed_message_content: source.fetch('content'),
             content_type: 'text', content_attributes: {}, status: 'sent', source_id: nil,
             message_type: source.fetch('direction'), private: source.fetch('private'),
             sender_type: source['direction'] == 'incoming' ? 'Contact' : nil,
             sender_id: source['direction'] == 'incoming' ? contact_id : nil,
             created_at: Time.iso8601(source.fetch('created_at')),
             updated_at: Time.iso8601(source.fetch('created_at')),
             additional_attributes: {KEY => {source: @source, message_id: source.fetch('id'),
                                             sha256: source.fetch('sha256')}}}
      message = existing[source.fetch('id')]
      if message
        info = message.additional_attributes.fetch(KEY)
        raise 'message_fingerprint_conflict' unless info['sha256'] == source.fetch('sha256')
        @counts[:messages_existing] += 1
      else
        message = insert(Message, row)
        @counts[:messages_created] += 1
      end
      # Read back the actual stored values, not merely the import marker/count.
      row.each do |key, expected|
        expected = expected.deep_stringify_keys if expected.is_a?(Hash)
        raise 'stored_message_mismatch' unless message.public_send(key) == expected
      end
      @counts[:messages_verified] += 1
      @counts[source.fetch('private') ? :private_notes : source.fetch('direction').to_sym] += 1
    end
  end
end

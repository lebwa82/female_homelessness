# Convert the verified isolated history inbox, without replaying any messages.
# Provider identity and a fresh backup are verified by the guarded caller.
class LegacyTelegramCutover
  KEY = 'legacy_telegram_cutover'.freeze

  def initialize(config)
    LegacyHistoryImporter.new(config) # Validate the same history format/source.
    @config = config
    @history = config.fetch('history')
    @account = Account.find(config.fetch('account_id'))
    @inbox = @account.inboxes.find(config.fetch('inbox_id'))
    @bot = AgentBot.find_by!(id: config.fetch('agent_bot_id'), account_id: @account.id)
    raise 'wrong_production_bot' unless config.fetch('bot_name') == 'nevidima_bot' &&
                                        config.fetch('bot_id') == 8831692160 &&
                                        config.fetch('token').start_with?('8831692160:')
    raise 'invalid_source_digest' unless config.fetch('source_sha256').match?(/\A[0-9a-f]{64}\z/)
  end

  def run
    report = nil
    ActiveRecord::Base.transaction(requires_new: true) do
      ActiveRecord::Base.connection.execute('SELECT pg_advisory_xact_lock(1846172041)')
      @inbox.lock!
      verify_history!
      if @inbox.channel_type == 'Channel::Telegram'
        raise 'different_production_channel' unless @inbox.channel.bot_name == @config['bot_name'] &&
                                                    @inbox.channel.bot_token == @config['token']
        raise 'missing_cutover_marker' unless @inbox.conversations.all? do |c|
          c.custom_attributes.dig(KEY, 'source_sha256') == @config['source_sha256']
        end
        # A repeated invocation must not steal conversations back from staff.
        report = result(already_promoted: true)
      else
        raise 'history_not_isolated' unless @inbox.channel_type == 'Channel::Api' &&
                                            @inbox.channel.additional_attributes[LegacyHistoryImporter::KEY] == @history['source'] &&
                                            @inbox.channel.webhook_url.blank? && !@inbox.agent_bot_inbox
        raise 'production_channel_already_exists' if Channel::Telegram.exists?(account_id: @account.id, bot_name: @config['bot_name'])
        now = Time.current
        channel_id = Channel::Telegram.insert_all!([{
          account_id: @account.id, bot_name: @config['bot_name'], bot_token: @config['token'],
          created_at: now, updated_at: now
        }], returning: %w[id]).rows.fetch(0).fetch(0)
        # Keep the old API channel for rollback; do not destroy any history.
        @inbox.update_columns(channel_type: 'Channel::Telegram', channel_id: channel_id,
                              name: 'Невидимые — Telegram', enable_auto_assignment: false,
                              lock_to_single_conversation: true, greeting_enabled: false,
                              csat_survey_enabled: false)
        AgentBotInbox.insert_all!([{
          account_id: @account.id, inbox_id: @inbox.id, agent_bot_id: @bot.id,
          status: AgentBotInbox.statuses.fetch('active'), created_at: now, updated_at: now
        }])
        @inbox.conversations.each do |conversation|
          user = @users.fetch(conversation.custom_attributes.dig(LegacyHistoryImporter::KEY, 'user_id'))
          attrs = conversation.custom_attributes.merge(
            'reply_owner' => 'bot', 'ownership_version' => 3,
            'ownership_last_staff_message_id' => conversation.messages.maximum(:id).to_i,
            'workflow_state' => 'open_conversation', 'workflow_need' => nil,
            'pending_aid_id' => nil, 'pending_contact_method' => nil,
            'pending_city' => nil, 'pending_district' => nil, 'pending_offer' => nil,
            'workflow_navigation' => {}, 'scenario' => {}, 'context_epoch' => 0,
            KEY => {'source_sha256' => @config['source_sha256'], 'cutover_at' => now.iso8601}
          )
          attrs[LegacyHistoryImporter::KEY] = attrs.fetch(LegacyHistoryImporter::KEY).merge(
            'legacy_state' => user.fetch('legacy_state'), 'unfinished' => !user.fetch('finished')
          )
          conversation.update_columns(status: Conversation.statuses.fetch('pending'),
                                      assignee_id: nil, assignee_agent_bot_id: nil,
                                      ai_assignee_type: nil, team_id: nil, custom_attributes: attrs)
        end
        @inbox.reload
        raise 'native_bot_binding_missing' unless @inbox.agent_bot_inbox&.agent_bot_id == @bot.id
        raise 'conversation_not_bot_ready' unless @inbox.conversations.all? do |c|
          c.pending? && c.assignee_id.nil? && c.assignee_agent_bot_id.nil? &&
            c.custom_attributes['reply_owner'] == 'bot' &&
            c.custom_attributes['workflow_state'] == 'open_conversation'
        end
        report = result(already_promoted: false)
      end
      raise ActiveRecord::Rollback unless @config.fetch('apply', false)
    end
    report
  end

  private

  def verify_history!
    @users = @history.fetch('users').to_h { |user| [user.fetch('id'), user] }
    raise 'duplicate_history_users' unless @users.size == @history['users'].size
    conversations = @inbox.conversations.to_a
    raise 'history_count_mismatch' unless conversations.size == @users.size &&
                                         @inbox.contact_inboxes.count == @users.size
    raise 'history_user_set_mismatch' unless conversations.map { |c| c.custom_attributes.dig(LegacyHistoryImporter::KEY, 'user_id') }.sort == @users.keys.sort
    expected_messages = @history['users'].sum { |u| u.fetch('messages').size }
    raise 'history_message_count_mismatch' unless @inbox.messages.count == expected_messages
    conversations.each do |conversation|
      metadata = conversation.custom_attributes[LegacyHistoryImporter::KEY] || {}
      user = @users.fetch(metadata['user_id']) { raise 'unrelated_conversation' }
      raise 'wrong_history_source' unless metadata['source'] == @history['source']
      link = conversation.contact_inbox
      raise 'telegram_contact_mismatch' unless link.inbox_id == @inbox.id &&
                                               link.source_id == user.fetch('telegram_id') &&
                                               conversation.additional_attributes['chat_id'] == user['telegram_id']
      messages = conversation.messages.to_a
      raise 'conversation_message_count_mismatch' unless messages.size == user['messages'].size
      by_id = messages.to_h { |m| [m.additional_attributes.dig(LegacyHistoryImporter::KEY, 'message_id'), m] }
      raise 'duplicate_history_messages' unless by_id.size == messages.size
      user['messages'].each do |source|
        message = by_id.fetch(source.fetch('id')) { raise 'missing_history_message' }
        raise 'history_fingerprint_mismatch' unless message.additional_attributes.dig(LegacyHistoryImporter::KEY, 'sha256') == source.fetch('sha256')
        raise 'history_text_mismatch' unless message.content == source.fetch('content')
      end
    end
  end

  def result(already_promoted:)
    {applied: @config.fetch('apply', false), already_promoted: already_promoted,
     inbox_id: @inbox.id, conversations: @inbox.conversations.count,
     messages: @inbox.messages.count,
     bot_owned: @inbox.conversations.count { |c| c.custom_attributes['reply_owner'] == 'bot' },
     telegram_contacts_preserved: true, historical_messages_replayed: false}
  end
end

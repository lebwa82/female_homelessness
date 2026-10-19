# Run with `rails runner` in the installed Chatwoot. All objects are unsaved.
# Only unrelated associations are stubbed; the three webhook serializers are real.
require 'ostruct'

class NevidimyProbePresenter < Conversations::EventDataPresenter
  private

  def push_messages
    []
  end

  def webhook_push_messages
    []
  end

  def push_meta
    {}
  end
end

rows = %w[housing food_money legal support children other].map.with_index do |need, index|
  inbox = Inbox.new(id: 3, name: 'In-memory contract probe', channel_type: 'Channel::Telegram')
  record = OpenStruct.new(
    inbox: inbox, display_id: 23, inbox_id: 3, additional_attributes: {},
    can_reply?: true, contact_inbox: nil, label_list: [], status: 'pending',
    custom_attributes: {}, unread_incoming_messages: [], waiting_since: nil,
    snoozed_until: nil, first_reply_created_at: nil, priority: nil,
    agent_last_seen_at: nil, contact_last_seen_at: nil, last_activity_at: nil,
    created_at: nil, updated_at: nil
  )
  presenter = NevidimyProbePresenter.new(record)
  account = OpenStruct.new(webhook_data: { id: 1 })
  account.define_singleton_method(:feature_enabled?) { |_name| false }
  presenter.define_singleton_method(:account) { account }
  message = Message.new(id: 41 + index, content: 'Нажата кнопка: «Тестовый выбор».',
                        message_type: :incoming, content_type: :text, private: false,
                        content_attributes: { 'nevidimy_telegram_callback_data' => "need:#{need}" })
  message.define_singleton_method(:inbox) { inbox }
  message.define_singleton_method(:conversation) { presenter }
  message.define_singleton_method(:account) { account }
  message.define_singleton_method(:sender) { OpenStruct.new(webhook_data: { id: 7 }) }
  payload = message.webhook_data
  # No model is persisted and no contact, message or credential is read.
  payload.slice(:id, :content, :content_attributes, :message_type, :private, :inbox, :sender)
         .merge(event: 'message_created',
                conversation: payload[:conversation].slice(:id, :channel),
                expected_action: "need:#{need}")
end
puts JSON.generate(rows)

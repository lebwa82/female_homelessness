# Neutral late-snapshot/HMAC probe; no Rails boot, network, or customer rows.
require 'stringio'
require 'openssl'
require 'time'
original_stdout = $stdout
begin
  $stdout = StringIO.new
  require_relative 'timeweb_agent_webhook_probe'
ensure
  $stdout = original_stdout
end

module NevidimyAtomicTurn
  CAPABILITY = 'nevidimy-atomic-turn-v1'.freeze
  def self.version(conversation)
    conversation.updated_at.utc.iso8601(6)
  end
end

Input = Struct.new(:id, :conversation_id, :account_id, :inbox_id, :sender_id,
                   :sender_type, :content_attributes, :private, keyword_init: true) do
  def incoming?; true; end
  def private?; self.private; end
end
ConversationRow = Struct.new(:id, :display_id, :account_id, :inbox_id, :contact_id, :updated_at,
                             :status, :custom_attributes, :assignee_id, :team_id, keyword_init: true)
class Message < Input
  class << self
    attr_accessor :row, :reads, :fail_read
    def uncached
      yield
    end
    def find_by(id:)
      self.reads += 1
      raise IOError, 'synthetic' if fail_read
      row if row && row.id == id
    end
  end
end
class Conversation < ConversationRow
  class << self
    attr_accessor :row, :reads
    def uncached
      yield
    end
    def find_by(id:)
      self.reads += 1
      row if row && row.id == id
    end
  end
end

class Webhooks::Trigger
  private
  def request_headers(body)
    { 'X-Chatwoot-Signature' => 'sha256=' + OpenSSL::HMAC.hexdigest('SHA256', @secret, '1.' + body) }
  end
end

def original_payload
  { event: 'message_created', id: 43, message_type: 'incoming', private: false, content: 'Button',
    content_attributes: { nevidimy_telegram_callback_data: 'need:legal' },
    conversation: { id: 23, channel: 'Channel::Telegram' }, sender: { id: 7 }, inbox: { id: 3 } }
end
def deliver(payload = original_payload)
  trigger = Webhooks::Trigger.new(ENV.fetch('NEVIDIMY_AGENT_WEBHOOK_URL'))
  trigger.instance_variable_set(:@payload, payload)
  trigger.execute
  verify(trigger.instance_variable_get(:@payload) == payload)
  request = $probe_http.requests.last
  expected = 'sha256=' + OpenSSL::HMAC.hexdigest('SHA256', 'test-only-signing-secret', '1.' + request.body)
  verify(request['X-Chatwoot-Signature'] == expected)
  JSON.parse(request.body)
end

ENV['NEVIDIMY_AGENT_BUTTON_SNAPSHOT'] = '1'
$probe_response = Net::HTTPOK.new('1.1', '200', 'OK')
Message.reads = Conversation.reads = 0
Message.row = Message.new(id: 43, conversation_id: 101, account_id: 2, inbox_id: 3,
                       sender_id: 7, sender_type: 'Contact', private: false,
                       content_attributes: { 'nevidimy_telegram_callback_data' => 'need:legal' })
Conversation.row = Conversation.new(id: 101, display_id: 23, account_id: 2, inbox_id: 3, contact_id: 7,
                                      updated_at: Time.utc(2026, 10, 22, 10, 0, 0, 123456), status: 'open',
                                      custom_attributes: { 'workflow_state' => 'synthetic' })
result = deliver
state = result.fetch('nevidimy_button_snapshot')
verify(state['schema'] == 1 && state['input_id'] == 43 && state['contact_id'] == 7 && state['inbox_id'] == 3)
verify(state['version'] == '2026-10-22T10:00:00.123456Z' && state['conversation']['id'] == 23)
verify(state['conversation']['custom_attributes'] == Conversation.row.custom_attributes)
verify(!state.key?('eligible') && !state.key?('reply_id') && Message.reads == 1 && Conversation.reads == 1)
report = { late_snapshot_exact_identity_two_reads_and_final_hmac: true }

# A new row/version at delivery wins over the earlier queued event.
Conversation.row.custom_attributes = { 'workflow_state' => 'changed' }
Conversation.row.updated_at += 1
verify(deliver.fetch('nevidimy_button_snapshot')['version'] == '2026-10-22T10:00:01.123456Z')
report[:fresh_row_not_early_listener_state] = true

mutations = [
  ->(p) { p[:id] = true }, ->(p) { p[:id] = 44 },
  ->(p) { p[:conversation][:id] = 24 }, ->(p) { p[:sender][:id] = 8 },
  ->(p) { p[:inbox][:id] = 4 }, ->(p) { p[:conversation][:channel] = 'Channel::Api' },
  ->(p) { p[:content_attributes][:nevidimy_telegram_callback_data] = 'other' },
  ->(p) { p[:private] = true }, ->(p) { p[:event] = 'message_updated' },
  ->(p) { p[:message_type] = 'outgoing' }
]
mutations.each do |change|
  value = original_payload
  change.call(value)
  verify(!deliver(value).key?('nevidimy_button_snapshot'))
end
report[:wrong_input_recipient_channel_event_or_action_falls_back] = true

[[Message.row, :private, true], [Message.row, :sender_type, 'User'],
 [Conversation.row, :contact_id, 8], [Conversation.row, :account_id, 9],
 [Conversation.row, :inbox_id, 4], [Conversation.row, :display_id, 24]].each do |row, key, value|
  old = row[key]
  row[key] = value
  verify(!deliver.key?('nevidimy_button_snapshot'))
  row[key] = old
end
report[:persisted_input_and_conversation_match] = true

before = Message.reads
trigger = Webhooks::Trigger.new(ENV.fetch('NEVIDIMY_AGENT_WEBHOOK_URL'), :api_inbox_webhook)
trigger.instance_variable_set(:@payload, original_payload)
trigger.execute
verify(trigger.fallback && Message.reads == before)
trigger = Webhooks::Trigger.new(ENV.fetch('NEVIDIMY_AGENT_WEBHOOK_URL') + '?unexpected=1')
trigger.instance_variable_set(:@payload, original_payload)
trigger.execute
verify(trigger.fallback && Message.reads == before)

Message.fail_read = true
verify(!deliver.key?('nevidimy_button_snapshot'))
Message.fail_read = false
Conversation.row.custom_attributes = { 'large' => 'x' * 262_144 }
verify(!deliver.key?('nevidimy_button_snapshot'))
Conversation.row.custom_attributes = {}
large = original_payload.merge(content: 'x' * 1_048_500)
verify(!deliver(large).key?('nevidimy_button_snapshot'))
ENV.delete('NEVIDIMY_AGENT_BUTTON_SNAPSHOT')
before = Message.reads
verify(!deliver.key?('nevidimy_button_snapshot') && Message.reads == before)
report[:errors_limits_and_disabled_preserve_original_event] = true
puts JSON.generate(report)

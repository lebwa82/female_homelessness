# Exact pinned pure native source; synthetic metadata only, no external requests.
require 'json'
require 'ostruct'
require 'securerandom'
require 'tempfile'

def verify(value, reason)
  raise reason unless value
end

class Object
  def blank?
    nil? || self == false || (respond_to?(:empty?) && empty?)
  end

  def present?
    !blank?
  end

  def try(name)
    public_send(name) if respond_to?(name)
  end
end

module AccessTokenable; end
module Avatarable; end
module WebhookSecretable; end
module Limits
  URL_LENGTH_LIMIT = 2048
end

class ApplicationRecord
  attr_accessor :id, :name, :outgoing_url, :secret

  class << self
    def scope(*); end
    def has_many(*); end
    def belongs_to(*); end
    def enum(*); end
    def validates(*); end
    def include_mod_with(*); end
  end
end

AGENT_BOT_SOURCE = <<~'RUBY'
# == Schema Information
#
# Table name: agent_bots
#
#  id           :bigint           not null, primary key
#  bot_config   :jsonb
#  bot_type     :integer          default("webhook")
#  description  :string
#  name         :string
#  outgoing_url :string
#  secret       :string
#  created_at   :datetime         not null
#  updated_at   :datetime         not null
#  account_id   :bigint
#
# Indexes
#
#  index_agent_bots_on_account_id  (account_id)
#

class AgentBot < ApplicationRecord
  include AccessTokenable
  include Avatarable

  include WebhookSecretable

  scope :accessible_to, lambda { |account|
    account_id = account&.id
    where(account_id: [nil, account_id])
  }

  has_many :agent_bot_inboxes, dependent: :destroy_async
  has_many :inboxes, through: :agent_bot_inboxes
  has_many :messages, as: :sender, dependent: :nullify
  has_many :platform_app_permissibles, as: :permissible, dependent: :destroy
  has_many :assigned_conversations, class_name: 'Conversation',
                                    foreign_key: :assignee_agent_bot_id,
                                    dependent: :nullify,
                                    as: :ai_assignee,
                                    inverse_of: :ai_assignee
  belongs_to :account, optional: true
  enum bot_type: { webhook: 0 }

  validates :outgoing_url, length: { maximum: Limits::URL_LENGTH_LIMIT }

  def available_name
    name
  end

  def push_event_data(inbox = nil)
    {
      id: id,
      name: name,
      avatar_url: avatar_url || inbox&.avatar_url,
      type: 'agent_bot'
    }
  end

  def webhook_data
    {
      id: id,
      name: name,
      type: 'agent_bot'
    }
  end

  def system_bot?
    account.nil?
  end
end

AgentBot.include_mod_with('Audit::AgentBot')
RUBY

MESSAGE_SOURCE = <<~'RUBY'
class Message
  def webhook_data
    data = {
      account: account.webhook_data,
      additional_attributes: additional_attributes,
      content_attributes: content_attributes,
      content_type: content_type,
      content: webhook_content,
      conversation: conversation.webhook_data,
      created_at: created_at,
      id: id,
      inbox: inbox.webhook_data,
      message_type: message_type,
      private: private,
      sender: sender.try(:webhook_data),
      source_id: source_id
    }
    data[:attachments] = attachments.map(&:push_event_data) if attachments.present?
    data
  end
end
RUBY

LISTENER_SOURCE = <<~'RUBY'
class AgentBotListener < BaseListener
  def conversation_resolved(event)
    conversation = extract_conversation_and_account(event)[0]
    inbox = conversation.inbox
    event_name = __method__.to_s
    payload = conversation.webhook_data.merge(event: event_name)
    agent_bots_for(inbox, conversation).each { |agent_bot| process_webhook_bot_event(agent_bot, payload) }
  end

  def conversation_opened(event)
    conversation = extract_conversation_and_account(event)[0]
    inbox = conversation.inbox
    event_name = __method__.to_s
    payload = conversation.webhook_data.merge(event: event_name)
    agent_bots_for(inbox, conversation).each { |agent_bot| process_webhook_bot_event(agent_bot, payload) }
  end

  def conversation_status_changed(event)
    conversation = extract_conversation_and_account(event)[0]
    changed_attributes = extract_changed_attributes(event)
    inbox = conversation.inbox
    event_name = __method__.to_s
    payload = conversation.webhook_data.merge(event: event_name, changed_attributes: changed_attributes)
    agent_bots_for(inbox, conversation).each { |agent_bot| process_webhook_bot_event(agent_bot, payload) }
  end

  def conversation_updated(event)
    conversation = extract_conversation_and_account(event)[0]
    changed_attributes = extract_changed_attributes(event)
    inbox = conversation.inbox
    event_name = __method__.to_s
    payload = conversation.webhook_data.merge(event: event_name, changed_attributes: changed_attributes)
    agent_bots_for(inbox, conversation).each { |agent_bot| process_webhook_bot_event(agent_bot, payload) }
  end

  def message_created(event)
    message = extract_message_and_account(event)[0]
    inbox = message.inbox
    return unless message.webhook_sendable?

    method_name = __method__.to_s
    agent_bots_for(inbox, message.conversation).each { |agent_bot| process_message_event(method_name, agent_bot, message, event) }
  end

  def message_updated(event)
    message = extract_message_and_account(event)[0]
    inbox = message.inbox
    return unless message.webhook_sendable?

    method_name = __method__.to_s
    agent_bots_for(inbox, message.conversation).each { |agent_bot| process_message_event(method_name, agent_bot, message, event) }
  end

  def webwidget_triggered(event)
    contact_inbox = event.data[:contact_inbox]
    inbox = contact_inbox.inbox
    event_name = __method__.to_s
    payload = contact_inbox.webhook_data.merge(event: event_name)
    payload[:event_info] = event.data[:event_info]
    agent_bots_for(inbox).each { |agent_bot| process_webhook_bot_event(agent_bot, payload) }
  end

  private

  def agent_bots_for(inbox, conversation = nil)
    bots = [active_inbox_agent_bot(inbox)]
    bots << conversation.ai_assignee if conversation&.assignee_type == 'AgentBot'

    bots.compact.uniq
  end

  def active_inbox_agent_bot(inbox)
    return unless inbox.agent_bot_inbox&.active?

    inbox.agent_bot
  end

  def process_message_event(method_name, agent_bot, message, _event)
    # Only webhook bots are supported
    payload = message.webhook_data.merge(event: method_name)
    process_webhook_bot_event(agent_bot, payload)
  end

  def process_webhook_bot_event(agent_bot, payload)
    return if agent_bot.outgoing_url.blank?

    AgentBots::WebhookJob.perform_later(agent_bot.outgoing_url, payload, :agent_bot_webhook,
                                        secret: agent_bot.secret, delivery_id: SecureRandom.uuid)
  end
end
RUBY

class ProbeEntity < OpenStruct
  def webhook_data
    raise native_error if native_error

    { id: id }
  end
end

class Message
  attr_accessor :message_type, :sender_type, :sender_id, :sender, :content_attributes, :private, :status
  attr_reader :serialization_calls, :inbox, :conversation

  def initialize(bot)
    @message_type, @sender_type, @sender_id, @sender = 'outgoing', 'AgentBot', bot.id, bot
    @content_attributes, @private, @serialization_calls = { 'bot_turn_key' => 'message:10' }, false, 0
    @inbox = ProbeEntity.new(id: 55, agent_bot: bot, agent_bot_inbox: OpenStruct.new(active?: true))
    @conversation = ProbeEntity.new(id: 101, inbox: @inbox, assignee_type: nil)
    @account = ProbeEntity.new(id: 9)
  end

  def account
    @serialization_calls += 1
    @account
  end

  def outgoing?
    message_type == 'outgoing'
  end

  def private?
    self.private
  end

  def webhook_sendable?
    true
  end

  def additional_attributes
    {}
  end

  def content_type
    'text'
  end

  def webhook_content
    'synthetic'
  end

  def created_at
    1
  end

  def id
    11
  end

  def source_id
    nil
  end

  def attachments
    []
  end
end

class BaseListener
  def extract_message_and_account(event)
    [event.message, nil]
  end

  def extract_conversation_and_account(event)
    [event.conversation, nil]
  end

  def extract_changed_attributes(_event)
    [{ status: 'synthetic' }]
  end
end

module AgentBots
  class WebhookJob
    def self.calls
      @calls ||= []
    end

    def self.perform_later(*arguments, **keywords)
      calls << [arguments, keywords]
      :native_job_return
    end
  end
end

module Rails
  class Reloader
    def to_prepare(&block)
      @prepares ||= []
      @prepares << block
    end

    def prepare
      @prepares.each(&:call)
    end
  end

  def self.application
    @application ||= OpenStruct.new(reloader: Reloader.new)
  end
end

def bot(id = 7, url = ENV['NEVIDIMY_AGENT_WEBHOOK_URL'])
  AgentBot.new.tap do |value|
    value.id, value.name, value.outgoing_url, value.secret = id, 'synthetic', url, 'synthetic'
  end
end

def dispatch(message, name = 'message_created')
  AgentBotListener.new.public_send(name, OpenStruct.new(message: message))
end

def verify_native(message, name = 'message_created')
  before = AgentBots::WebhookJob.calls.size
  dispatch(message, name)
  verify(AgentBots::WebhookJob.calls.size == before + 1 && message.serialization_calls == 1,
         'nonqualifying event lost native serialization/job')
  arguments, keywords = AgentBots::WebhookJob.calls.last
  verify(arguments[1][:event] == name && arguments[2] == :agent_bot_webhook &&
         keywords.keys.sort == [:delivery_id, :secret], 'native job contract changed')
end

ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = "https://agent-bot:8080/webhooks/chatwoot/agent/#{'a' * 32}"
report = {}
Tempfile.create(['native-agent-bot', '.rb']) do |agent_source|
  Tempfile.create(['native-message-serializer', '.rb']) do |message_source|
    Tempfile.create(['native-agent-bot-listener', '.rb']) do |listener_source|
      [[agent_source, AGENT_BOT_SOURCE], [message_source, MESSAGE_SOURCE], [listener_source, LISTENER_SOURCE]].each do |file, source|
        file.write(source)
        file.flush
        load file.path
      end
      initializer = File.expand_path('../deploy/timeweb/agent_bot_self_event_filter.rb', __dir__)
      load initializer
      verify(NevidimyAgentBotSelfEventFilter.native_contract?(AgentBotListener), 'exact native contract not recognized')
      listener_source.write("# unsupported source\n")
      listener_source.flush
      Rails.application.reloader.prepare
      verify(!(AgentBotListener < NevidimyAgentBotSelfEventFilter::Listener), 'unsupported listener source enabled')
      listener_source.truncate(0)
      listener_source.rewind
      listener_source.write(LISTENER_SOURCE)
      listener_source.flush
      Rails.application.reloader.prepare
      report[:pinned_native_contract_gate] = true

      [false, true, :alert].each do |kind|
        message = Message.new(bot)
        if kind
          message.private = true
          prefix = kind == :alert ? 'scenario-alert' : 'scenario'
          message.content_attributes = { 'bot_event_key' => "#{prefix}:#{'a' * 24}" }
        end
        before = AgentBots::WebhookJob.calls.size
        dispatch(message)
        verify(message.serialization_calls.zero? && AgentBots::WebhookJob.calls.size == before,
               'marked self-created event serialized or enqueued')
      end
      report[:only_owned_self_created_markers_are_skipped] = true

      other = bot(8, 'https://other.invalid/synthetic')
      message = Message.new(bot)
      message.conversation.assignee_type = 'AgentBot'
      message.conversation.ai_assignee = other
      verify_native(message)
      verify(AgentBots::WebhookJob.calls.last[0][0] == other.outgoing_url, 'another target bot was skipped')
      message = Message.new(bot)
      message.conversation.assignee_type = 'AgentBot'
      message.conversation.ai_assignee = bot(8)
      verify_native(message)
      report[:per_target_identity_preserves_other_bots] = true

      %w[incoming activity].each do |type|
        message = Message.new(bot)
        message.message_type = type
        verify_native(message)
      end
      message = Message.new(bot)
      message.sender_type = 'User'
      message.sender = OpenStruct.new(webhook_data: { id: 7, type: 'user' })
      verify_native(message)
      message = Message.new(bot)
      verify_native(message, 'message_updated')
      report[:incoming_staff_and_delivery_updates_remain_native] = true

      [nil, '7', 0, -1, 8, true].each do |id|
        message = Message.new(bot)
        message.sender_id = id
        verify_native(message)
      end
      [nil, '7', 0, -1, true].each { |id| verify_native(Message.new(bot(id))) }
      [nil, {}, [], 'invalid', { 'bot_turn_key' => 'message:0' },
       { 'bot_turn_key' => 'message:010' }, { 'bot_turn_key' => 'message:10\n' },
       { 'bot_turn_key' => "message:#{'1' * 20}" }, { 'bot_event_key' => "scenario:#{'a' * 24}" }].each do |attributes|
        message = Message.new(bot)
        message.content_attributes = attributes
        verify_native(message)
      end
      [nil, {}, { 'bot_turn_key' => 'message:10' }, { 'bot_event_key' => "scenario:#{'A' * 24}" }].each do |attributes|
        message = Message.new(bot)
        message.private, message.content_attributes = true, attributes
        verify_native(message)
      end
      report[:unknown_identity_and_metadata_fall_back] = true

      approved = ENV['NEVIDIMY_AGENT_WEBHOOK_URL']
      [nil, '', approved.sub('https:', 'http:'), approved + '?x=synthetic', approved + '#synthetic',
       approved.sub('agent-bot:8080', 'other:8080'), approved.sub('/agent/', '/other/'),
       approved.sub('agent-bot:', 'user@agent-bot:')].each do |url|
        ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = url
        verify_native(Message.new(bot(7, approved)))
      end
      ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved
      verify_native(Message.new(bot(7, approved + 'b')))
      message = Message.new(bot(7, nil))
      before = AgentBots::WebhookJob.calls.size
      dispatch(message)
      verify(message.serialization_calls == 1 && AgentBots::WebhookJob.calls.size == before,
             'blank target URL did not preserve native serialization/no-job behavior')
      report[:exact_approved_internal_https_url_required] = true

      message = Message.new(bot)
      message.sender.define_singleton_method(:webhook_data) { { id: id, type: 'user' } }
      verify_native(message)
      message = Message.new(bot)
      message.define_singleton_method(:webhook_data) { { sender: { type: 'user' } } }
      before = AgentBots::WebhookJob.calls.size
      dispatch(message)
      verify(AgentBots::WebhookJob.calls.size == before + 1, 'unexpected message serializer was skipped')
      report[:unexpected_staff_sender_or_serializer_never_skipped] = true

      original_sender = AgentBot.instance_method(:webhook_data)
      begin
        AgentBot.send(:define_method, :webhook_data) { { id: id, name: name, type: 'user' } }
        message = Message.new(bot)
        verify_native(message)
        verify(AgentBots::WebhookJob.calls.last[0][1][:sender][:type] == 'user', 'staff-shaped sender was discarded')
        AgentBot.send(:remove_method, :webhook_data)
        verify_native(Message.new(bot))
        verify(AgentBots::WebhookJob.calls.last[0][1][:sender].nil?, 'unsupported sender contract did not stay native')
      ensure
        AgentBot.send(:define_method, :webhook_data, original_sender)
      end
      before = AgentBots::WebhookJob.calls.size
      dispatch(Message.new(bot))
      verify(AgentBots::WebhookJob.calls.size == before, 'restoring exact native sender did not restore bounded skip')

      message = Message.new(bot)
      message.content_attributes = {}
      failure = IOError.new('synthetic native failure')
      message.account.native_error = failure
      before = AgentBots::WebhookJob.calls.size
      begin
        dispatch(message)
        raise 'native serializer error was swallowed'
      rescue IOError => caught
        verify(caught.equal?(failure), 'native serializer error identity changed')
      end
      verify(AgentBots::WebhookJob.calls.size == before, 'native serializer error still enqueued')
      report[:native_exceptions_preserved] = true

      load initializer
      2.times { Rails.application.reloader.prepare }
      verify(AgentBotListener.ancestors.count(NevidimyAgentBotSelfEventFilter::Listener) == 1, 'reload duplicated filter')
      verify(AgentBotListener.private_instance_methods.include?(:process_message_event), 'native helper visibility changed')
      report[:reload_idempotency_and_private_visibility] = true
    end
  end
end
puts JSON.generate(report)

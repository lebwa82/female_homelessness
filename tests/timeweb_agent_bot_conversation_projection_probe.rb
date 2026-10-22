# Neutral Ruby fan-out and fail-closed contracts; no Rails/database/network.
require 'json'
require 'ostruct'
require 'pathname'
require 'tmpdir'
require 'securerandom'

def verify(value, code)
  raise code unless value
end

module Rails
  def self.root
    @root
  end

  def self.application
    OpenStruct.new(reloader: self)
  end

  def self.to_prepare(&block)
    @prepare = block
  end
end

module PushDataHelper
  def webhook_data
    self.serializations += 1
    raise 'serializer_failure' if fail_serialization
    { id: display_id, status: 'pending', custom_attributes: { 'reply_owner' => 'bot' },
      messages: [{ id: 70, content: 'neutral' }] }
  end
end

class Conversation
  include PushDataHelper
  attr_accessor :serializations, :recipients, :fail_serialization

  def initialize(recipients)
    @recipients, @serializations = recipients, 0
  end

  def display_id
    12
  end

  def inbox
    self
  end
end

module Conversations
  class EventDataPresenter
    def webhook_data; end
    def push_data; end
    def push_messages; end
    def webhook_push_messages; end
  end
end

class AgentBot
  module GeneratedAttributeMethods
    attr_accessor :outgoing_url
  end
  include GeneratedAttributeMethods
  attr_reader :id

  def initialize(id, outgoing_url)
    @id, @outgoing_url = id, outgoing_url
  end

  def webhook_data
    { id: id }
  end

  def secret
    'neutral-test-signing-secret'
  end
end

module AgentBots
  class WebhookJob
    class << self
      attr_accessor :jobs, :queue, :failure

      def set(queue:)
        self.queue = queue
        self
      end

      def perform_later(*args, **options)
        raise failure if failure

        (self.jobs ||= []) << { args: args, options: options, queue: queue }
      end
    end
  end
end

class BaseListener
  def extract_conversation_and_account(event)
    [event.data[:conversation], nil]
  end

  def extract_changed_attributes(event)
    changes = event.data[:changed_attributes]
    return if changes.nil? || changes.empty?

    changes.map { |key, values| { key => { previous_value: values[0], current_value: values[1] } } }
  end
end

class AgentBotListener < BaseListener
  attr_reader :jobs

  def initialize
    @jobs = []
  end

  def conversation_updated(event)
    conversation = extract_conversation_and_account(event)[0]
    changed_attributes = extract_changed_attributes(event)
    payload = conversation.webhook_data.merge(event: 'conversation_updated', changed_attributes: changed_attributes)
    agent_bots_for(conversation.inbox, conversation).each { |bot| process_webhook_bot_event(bot, payload) }
  end

  private

  def agent_bots_for(inbox, _conversation = nil)
    inbox.recipients
  end

  def active_inbox_agent_bot(inbox)
    inbox.recipients.first
  end

  def process_webhook_bot_event(bot, payload)
    return if bot.outgoing_url.nil? || bot.outgoing_url.empty?

    @jobs << { bot_id: bot.id, url: bot.outgoing_url, payload: payload, type: :agent_bot_webhook,
               delivery_id: SecureRandom.uuid }
  end
end

module NevidimyTurnTiming
  module BotEvents
    def conversation_updated(event)
      super
    end
  end
end

module NevidimyWebhookPresenterFastPath
  module Presenter
    def webhook_data
      super
    end

    def push_messages
      super
    end
  end
end

AgentBotListener.prepend(NevidimyTurnTiming::BotEvents)
Conversations::EventDataPresenter.prepend(NevidimyWebhookPresenterFastPath::Presenter)

Dir.mktmpdir('conversation-projection-') do |directory|
  Rails.instance_variable_set(:@root, Pathname.new(directory))
  File.write(File.join(directory, 'VERSION_CW'), '4.19.0')
  load File.expand_path('../deploy/timeweb/agent_bot_conversation_projection.rb', __dir__)
  projection = NevidimyAgentBotConversationProjection
  verify(projection.verified_contract.nil?, 'unknown_sources_accepted')
  digest = Digest::SHA256.file(__FILE__).hexdigest
  owners = projection.method_specs.flat_map { |type, name, _| projection.chain(type.instance_method(name)).map { |m| m.owner.name } }
  projection.send(:remove_const, :OWNER_SHA256S)
  wrappers = projection::WRAPPER_SHA256S.keys
  projection.const_set(:OWNER_SHA256S, (owners.uniq - wrappers).to_h { |name| [name, digest] }.freeze)
  projection.send(:remove_const, :WRAPPER_SHA256S)
  projection.const_set(:WRAPPER_SHA256S, wrappers.to_h { |name| [name, digest] }.freeze)
  projection.install
  verify(AgentBotListener < projection::Listener, 'known_contract_not_installed')
  projection.install
  verify(AgentBotListener.ancestors.count(projection::Listener) == 1, 'reload_duplicated_wrapper')

  approved = 'https://agent-bot:8080/webhooks/chatwoot/agent/' + ('p' * 32)
  ENV['NEVIDIMY_AGENT_WEBHOOK_URL'] = approved
  internal = AgentBot.new(1, approved)
  external = AgentBot.new(2, 'https://external.invalid/webhook')
  other = AgentBot.new(3, 'https://other.invalid/webhook')
  listener = AgentBotListener.new
  verify(!projection.verified_contract.nil?, 'repeat_contract')
  contract = projection.instance_variable_get(:@contract)
  contract.each do |(type, name), expected|
    verify(projection.chain(type.instance_method(name)) == expected, "class_contract_#{type}_#{name}")
  end
  projection::LISTENER_METHODS.each_key do |name|
    verify(projection.chain(listener.method(name)) == contract[[AgentBotListener, name]], "instance_contract_#{name}")
  end
  verify(projection.same_receiver_chain?(Conversation.new([internal]), Conversation, :webhook_data), 'conversation_contract')
  verify(projection.current_contract?(listener, Conversation.new([internal])), 'receiver_contract')
  cases = []
  deltas = [
    nil,
    { 'waiting_since' => ['2026-10-22T10:00:00Z', nil] },
    { 'status' => ['pending', 'open'], 'assignee_id' => [nil, 7] },
    { 'custom_attributes' => [{ 'workflow_state' => 'before' }, { 'workflow_state' => 'after' }] },
    { 'custom_attributes' => [{ 'reply_owner' => 'bot', 'context_epoch' => 0 },
                              { 'reply_owner' => 'human', 'context_epoch' => 1 }] },
    { 'custom_attributes' => [{ 'unknown' => { 'flag' => true } }, { 'unknown' => { 'flag' => 1 } }] }
  ]
  deltas.each do |delta|
    conversation = Conversation.new([internal, external])
    event = OpenStruct.new(data: { conversation: conversation, changed_attributes: delta })
    ENV[projection::OPT_IN] = '0'
    listener.jobs.clear
    listener.conversation_updated(event)
    baseline = JSON.parse(JSON.generate(listener.jobs))
    ENV[projection::OPT_IN] = '1'
    listener.jobs.clear
    conversation.serializations = 0
    listener.conversation_updated(event)
    projected = JSON.parse(JSON.generate(listener.jobs))
    verify(conversation.serializations == 1, 'mixed_fanout_not_lazy_once')
    verify(projected.length == baseline.length && projected.map { |job| job['bot_id'] } == [1, 2], 'recipient_order_changed')
    baseline.zip(projected).each do |before, after|
      verify(before.reject { |key, _| %w[payload delivery_id].include?(key) } ==
             after.reject { |key, _| %w[payload delivery_id].include?(key) }, 'job_contract_changed')
      verify(after['delivery_id'].match?(/\A[0-9a-f-]{36}\z/), 'delivery_id_missing')
    end
    verify(projected[0]['payload'].keys.sort == %w[changed_attributes event id], 'projection_keys')
    verify(projected[1]['payload'] == baseline[1]['payload'], 'external_payload_changed')
    cases << { baseline: baseline[0]['payload'], projected: projected[0]['payload'] }
  end

  [[internal], [external, other], [AgentBot.new(4, approved + '/extra')]].each do |bots|
    conversation = Conversation.new(bots)
    listener.jobs.clear
    listener.conversation_updated(OpenStruct.new(data: { conversation: conversation }))
    verify(conversation.serializations == (bots == [internal] ? 0 : 1), 'serialization_count')
  end
  singleton = AgentBot.new(5, approved)
  singleton.define_singleton_method(:outgoing_url) { approved }
  verify(!projection.internal_recipient?(singleton), 'unknown_accessor_compacted')
  verify(!projection.internal_recipient?(Class.new(AgentBot).new(6, approved)), 'subclass_compacted')
  %w[0 1].each do |enabled|
    ENV[projection::OPT_IN] = enabled
    conversation = Conversation.new([internal, external])
    conversation.fail_serialization = true
    listener.jobs.clear
    begin
      listener.conversation_updated(OpenStruct.new(data: { conversation: conversation }))
      raise 'serializer_failure_missing'
    rescue RuntimeError => error
      verify(error.message == 'serializer_failure', 'serialization_error_changed')
      verify(listener.jobs.empty? && conversation.serializations == 1, 'partial_fanout_before_serialization_failure')
    end
  end
  ENV[projection::OPT_IN] = '1'
  # Waiting-timer events remain durable, signed jobs; only their queue changes.
  # Other deltas and recipients must retain native high-priority delivery.
  ENV[projection::BACKGROUND_WAITING] = '1'
  now, later = Time.utc(2026, 10, 22, 10), Time.utc(2026, 10, 22, 11)
  valid = { 'waiting_since' => [now, nil], 'updated_at' => [now, later] }
  variants = [valid, valid.merge('waiting_since' => [nil, now])]
  variants.each do |delta|
    conversation = Conversation.new([internal, external])
    event = OpenStruct.new(data: { conversation: conversation, changed_attributes: delta })
    listener.jobs.clear
    AgentBots::WebhookJob.jobs = []
    listener.conversation_updated(event)
    verify(listener.jobs.length == 1 && listener.jobs.first[:bot_id] == external.id, 'external_priority_changed')
    captured = AgentBots::WebhookJob.jobs
    verify(captured.length == 1 && captured.first[:queue] == :medium, 'waiting_job_missing_or_wrong_queue')
    job = captured.first
    verify(job[:args][0] == approved && job[:args][2] == :agent_bot_webhook, 'waiting_target_or_type_changed')
    verify(job[:options][:secret] == internal.secret && job[:options][:delivery_id].match?(/\A[0-9a-f-]{36}\z/), 'waiting_signature_identity_changed')
    verify(job[:args][1] == { id: conversation.display_id, event: 'conversation_updated',
                            changed_attributes: listener.send(:extract_changed_attributes, event) }, 'waiting_payload_changed')
  end
  rejected = [nil, {}, valid.reject { |key, _| key == 'updated_at' },
              valid.merge('status' => ['pending', 'open']), valid.merge('team_id' => [nil, 7]),
              valid.merge('assignee_id' => [nil, 9]), valid.merge('unknown' => [false, true]),
              valid.merge('custom_attributes' => [{}, { 'reply_owner' => 'human' }]),
              valid.merge('waiting_since' => [now, later]), valid.merge('waiting_since' => [nil, nil]),
              valid.merge('waiting_since' => ['not-a-time', nil]), valid.merge('updated_at' => [nil, later])]
  rejected.each do |delta|
    listener.jobs.clear
    AgentBots::WebhookJob.jobs = []
    listener.conversation_updated(OpenStruct.new(data: { conversation: Conversation.new([internal]), changed_attributes: delta }))
    verify(listener.jobs.length == 1 && AgentBots::WebhookJob.jobs.empty?, 'unknown_or_important_delta_reprioritized')
  end
  verify(!projection.waiting_only?([{ 'waiting_since' => { previous_value: now, current_value: nil } }] * 2), 'duplicate_delta_reprioritized')
  %w[0 1].each do |enabled|
    ENV[projection::BACKGROUND_WAITING] = enabled
    listener.jobs.clear
    AgentBots::WebhookJob.jobs = []
    AgentBots::WebhookJob.failure = 'enqueue_failed' if enabled == '1'
    begin
      listener.conversation_updated(OpenStruct.new(data: { conversation: Conversation.new([internal]), changed_attributes: valid }))
      verify(enabled == '0' && listener.jobs.length == 1, 'enqueue_failure_swallowed')
    rescue RuntimeError => error
      verify(error.message == 'enqueue_failed' && listener.jobs.empty? && AgentBots::WebhookJob.jobs.empty?, 'enqueue_retried_or_failure_changed')
    ensure
      AgentBots::WebhookJob.failure = nil
    end
  end
  ENV[projection::BACKGROUND_WAITING] = '0'
  fallback = Conversation.new([internal])
  fallback.define_singleton_method(:webhook_data) { super() }
  listener.conversation_updated(OpenStruct.new(data: { conversation: fallback }))
  verify(fallback.serializations == 1, 'unknown_serializer_compacted')
  File.write(File.join(directory, 'VERSION_CW'), '4.20.0')
  projection.install
  verify(projection.verified_contract.nil?, 'unknown_version_accepted')
  fallback = Conversation.new([internal])
  listener.conversation_updated(OpenStruct.new(data: { conversation: fallback }))
  verify(fallback.serializations == 1, 'unknown_version_not_native')
  File.write(File.join(directory, 'VERSION_CW'), '4.19.0')
  projection.install
  unknown = Module.new { def extract_changed_attributes(event); super; end }
  AgentBotListener.prepend(unknown)
  verify(projection.verified_contract.nil?, 'unknown_owner_accepted')
  fallback = Conversation.new([internal])
  listener.conversation_updated(OpenStruct.new(data: { conversation: fallback }))
  verify(fallback.serializations == 1, 'changed_method_not_native')
  puts JSON.generate(ok: true, native_fanout_preserved: true, source_guards: true, cases: cases)
end

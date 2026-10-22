# Neutral structural fan-out/source contracts; no Rails/database/network.
require 'json'
require 'ostruct'
require 'pathname'
require 'tmpdir'
require 'securerandom'

def verify(value, code)
  raise code unless value
end

module Rails
  module VERSION
    STRING = '7.2.3.1'.freeze
  end
  def self.root; @root; end
  def self.application; OpenStruct.new(config: self, reloader: self); end
  def self.after_initialize(&block); @after_initialize = block; end
  def self.to_prepare(&block); @prepare = block; end
end

class AgentBot
  module GeneratedAttributeMethods
    attr_accessor :outgoing_url
  end
  include GeneratedAttributeMethods
  attr_reader :id
  def initialize(id, url); @id, self.outgoing_url = id, url; end
end

class Contact
  def id; 11; end
  def webhook_data; { id: id, type: 'contact', name: 'Neutral' }; end
end

class Inbox
  attr_accessor :channel_type
  def initialize; @channel_type = 'Channel::Telegram'; end
  def id; 12; end
  def webhook_data; { id: id, name: 'Neutral' }; end
end

class Conversation
  attr_reader :inbox
  def initialize(inbox); @inbox = inbox; end
  def display_id; 13; end
  def inbox_id; inbox.id; end
  def webhook_data
    { id: display_id, channel: inbox.channel_type, messages: [{ id: 14, content: 'Neutral history' }],
      custom_attributes: { reply_owner: 'bot' }, unread_count: 1 }
  end
end

module MessageFilterHelpers
  def webhook_sendable?; incoming? || outgoing?; end
end

class Message
  include MessageFilterHelpers
  attr_accessor :message_type, :private, :content_type, :content_attributes, :sender, :fail_serialization, :serializations, :fail_on_serialization
  attr_reader :inbox, :conversation
  def initialize
    @inbox = Inbox.new
    @conversation = Conversation.new(@inbox)
    @message_type, @private, @content_type, @sender = 'incoming', false, 'text', Contact.new
    @content_attributes = { 'nevidimy_telegram_callback_data' => 'menu:next' }
    @serializations = 0
  end
  def id; 15; end
  def inbox_id; inbox.id; end
  def incoming?; message_type == 'incoming'; end
  def outgoing?; message_type == 'outgoing'; end
  def private?; @private; end
  def webhook_content; 'Neutral callback label'; end
  def webhook_data
    self.serializations += 1
    raise 'neutral_serializer_failure' if fail_serialization || serializations == fail_on_serialization
    { id: id, message_type: message_type, private: private?, content: webhook_content,
      content_attributes: content_attributes, sender: sender.webhook_data, inbox: inbox.webhook_data,
      conversation: conversation.webhook_data, full_native: true }
  end
end

class AgentBotListener
  attr_accessor :bots
  attr_reader :jobs
  def initialize(bots); @bots, @jobs = bots, []; end
  def message_created(event)
    message = extract_message_and_account(event)[0]
    inbox = message.inbox
    return unless message.webhook_sendable?
    method_name = __method__.to_s
    agent_bots_for(inbox, message.conversation).each { |bot| process_message_event(method_name, bot, message, event) }
  end
  private
  def extract_message_and_account(event); [event.data[:message], nil]; end
  def agent_bots_for(_inbox, _conversation = nil); bots; end
  def process_message_event(name, bot, message, _event)
    process_webhook_bot_event(bot, message.webhook_data.merge(event: name))
  end
  def process_webhook_bot_event(bot, payload)
    return if bot.outgoing_url.nil? || bot.outgoing_url.empty?
    jobs << { job: 'AgentBots::WebhookJob', recipient: bot.id, url: bot.outgoing_url, payload: payload,
              queue: 'high', webhook_type: 'agent_bot_webhook', signature: 'native-test-signing-contract',
              delivery_id: SecureRandom.uuid }
  end
end

module NevidimyAgentBotSelfEventFilter
  def self.skip?(_name, bot, message); message.outgoing? && bot.id == 1; end
  module Listener
    private
    def process_message_event(name, bot, message, event)
      return if NevidimyAgentBotSelfEventFilter.skip?(name, bot, message)
      super
    end
  end
end
AgentBotListener.prepend(NevidimyAgentBotSelfEventFilter::Listener)

module NevidimyAgentBotConversationProjection
  module Listener; end
  def self.current_contract?(_listener, _conversation); @valid != false; end
  def self.internal_recipient?(bot)
    bot.instance_of?(AgentBot) && bot.id.is_a?(Integer) && bot.id.positive? &&
      bot.method(:outgoing_url).owner == AgentBot::GeneratedAttributeMethods &&
      bot.outgoing_url == 'https://agent-bot:8080/webhooks/chatwoot/agent/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  end
end
AgentBotListener.prepend(NevidimyAgentBotConversationProjection::Listener)

module NevidimyTurnTiming
  def self.measure(name)
    @spans ||= []
    @spans << name
    yield
  end
  def self.spans; @spans ||= []; end
  module BotEvents
    def message_created(event)
      NevidimyTurnTiming.measure(:bot_message_created) { super(event) }
    end
  end
end
AgentBotListener.prepend(NevidimyTurnTiming::BotEvents)

def contract_jobs(jobs)
  jobs.map { |job| job.reject { |key, _| %i[payload delivery_id].include?(key) } }
end

path = File.expand_path('../deploy/timeweb/agent_bot_message_projection.rb', __dir__)
Dir.mktmpdir('nevidimy-message-projection-') do |directory|
  Rails.instance_variable_set(:@root, Pathname.new(directory))
  File.write(File.join(directory, 'VERSION_CW'), '4.19.0')
  require path
  projection = NevidimyAgentBotMessageProjection
  checks = {}
  ENV.delete(projection::OPT_IN)
  checks[:off_by_default] = !projection.install && !(AgentBotListener < projection::Listener)
  ENV[projection::OPT_IN] = '1'
  checks[:unknown_sources_rejected] = !projection.install && !(AgentBotListener < projection::Listener)
  digest = Digest::SHA256.file(__FILE__).hexdigest
  owners = projection::SOURCE_SHA256S.keys
  projection.send(:remove_const, :SOURCE_SHA256S)
  projection.const_set(:SOURCE_SHA256S, owners.to_h { |name| [name, digest] }.freeze)
  File.write(File.join(directory, 'VERSION_CW'), '4.20.0')
  checks[:unknown_version_rejected] = !projection.install
  File.write(File.join(directory, 'VERSION_CW'), '4.19.0')
  original = Message.instance_method(:webhook_content)
  Message.send(:define_method, :webhook_content) { |required| required }
  checks[:unknown_arity_rejected] = !projection.install
  Message.send(:define_method, :webhook_content, original)
  checks[:installed_late] = projection.install && Rails.instance_variable_get(:@after_initialize).respond_to?(:call)
  native = AgentBotListener.instance_method(:message_created).super_method
  internal = AgentBot.new(1, 'https://agent-bot:8080/webhooks/chatwoot/agent/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
  external = AgentBot.new(2, 'https://example.invalid/native-webhook')
  another_internal = AgentBot.new(3, internal.outgoing_url)
  %i[message sender inbox listener].each do |name|
    message, listener = Message.new, AgentBotListener.new([internal])
    receiver = { message: message, sender: message.sender, inbox: message.inbox, listener: listener }.fetch(name)
    receiver.singleton_class
    verify(projection.current_contract?(listener, message), 'harmless_singleton_rejected')
    listener.message_created(OpenStruct.new(data: { message: message }))
    verify(message.serializations.zero? && !listener.jobs.first[:payload][:full_native], 'harmless_singleton_fell_back')
  end
  checks[:harmless_singletons_preserve_exact_dispatch] = true
  override_specs = [[Message, :webhook_data, :message], [Message, :webhook_content, :message],
                    [Message, :webhook_sendable?, :message], [Contact, :webhook_data, :sender],
                    [Inbox, :webhook_data, :inbox], [AgentBotListener, :nevidimy_project_callback, :listener]]
  override_specs.each do |type, name, target|
    %i[singleton prepend].each do |kind|
      message, listener = Message.new, AgentBotListener.new([internal])
      receiver = { message: message, sender: message.sender, inbox: message.inbox, listener: listener }.fetch(target)
      original = type.instance_method(name)
      if kind == :singleton
        receiver.define_singleton_method(name, original)
      else
        wrapper = Module.new
        wrapper.send(:define_method, name) { |*args| super(*args) }
        receiver.singleton_class.prepend(wrapper)
      end
      verify(!projection.current_contract?(listener, message), 'unknown_receiver_dispatch_accepted')
    end
  end
  checks[:singleton_definitions_and_prepends_fail_closed] = true
  cases = []
  [[:internal, [internal]], [:external, [external]], [:mixed, [internal, external]],
   [:mixed_reversed, [external, internal]], [:multiple_internal, [internal, another_internal]], [:empty, []]].each do |name, bots|
    message = Message.new
    event = OpenStruct.new(data: { message: message })
    listener = AgentBotListener.new(bots)
    NevidimyTurnTiming.spans.clear
    native.bind(listener).call(event)
    baseline = listener.jobs.dup
    listener.jobs.clear
    message.serializations = 0
    projection_spans = NevidimyTurnTiming.spans.length
    listener.message_created(event)
    projected = listener.jobs.dup
    verify(NevidimyTurnTiming.spans.length == projection_spans + 1, 'timing_span_changed')
    verify(contract_jobs(baseline) == contract_jobs(projected), 'job_contract_changed')
    verify(baseline.map { |job| job[:recipient] } == projected.map { |job| job[:recipient] }, 'fanout_changed')
    verify(projected.select { |job| job[:recipient] == 2 }.map { |job| job[:payload] } ==
           baseline.select { |job| job[:recipient] == 2 }.map { |job| job[:payload] }, 'external_payload_changed')
    verify(projected.map { |job| job[:delivery_id] }.uniq.length == projected.length &&
           projected.all? { |job| job[:delivery_id].match?(/\A[0-9a-f-]{36}\z/) }, 'delivery_uuid_changed')
    verify(message.serializations == (name == :internal ? 0 : bots.length), 'serialization_count')
    verify(baseline.map { |job| job[:payload] } == projected.map { |job| job[:payload] }, 'fallback_payload_changed') unless name == :internal
    base_internal = baseline.find { |job| job[:recipient] == 1 }
    projected_internal = projected.find { |job| job[:recipient] == 1 }
    cases << { name: name, baseline: base_internal[:payload], projected: projected_internal[:payload] } if base_internal
  end
  checks[:native_fanout_and_external_jobs_equal] = true
  checks[:one_timing_span] = true
  checks[:internal_only_skips_full_serializer] = true
  checks[:mixed_keeps_native_per_recipient_serialization] = true
  changed_recipient = AgentBot.new(1, internal.outgoing_url)
  changed_recipient.define_singleton_method(:outgoing_url) { 'https://agent-bot:8080/webhooks/chatwoot/agent/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }
  listener, message = AgentBotListener.new([changed_recipient]), Message.new
  listener.message_created(OpenStruct.new(data: { message: message }))
  checks[:unknown_recipient_accessor_falls_back] = listener.jobs.first[:payload][:full_native] == true

  # All cases outside the exact callback contract still execute the native body.
  variants = [proc { |m| m.content_attributes = {} }, proc { |m| m.content_attributes = { 'nevidimy_telegram_callback_data' => '' } },
              proc { |m| m.private = true }, proc { |m| m.content_type = 'input_select' },
              proc { |m| m.inbox.channel_type = 'Channel::Api' },
              proc { |m| m.conversation.define_singleton_method(:inbox_id) { 99 } },
              proc { |m| m.content_attributes = { 'nevidimy_telegram_callback_data' => 'x' * 65 } },
              proc { |m| m.content_attributes = { 'nevidimy_telegram_callback_data' => 1 } },
              proc { |m| m.define_singleton_method(:webhook_content) { super() } },
              proc { |m| m.define_singleton_method(:webhook_sendable?) { true } },
              proc { |m| m.sender.define_singleton_method(:webhook_data) { super() } }]
  variants.each do |change|
    message = Message.new
    change.call(message)
    listener = AgentBotListener.new([internal])
    listener.message_created(OpenStruct.new(data: { message: message }))
    verify(listener.jobs.first[:payload][:full_native] == true && message.serializations == 1, 'unknown_scope_not_native')
  end
  checks[:unknown_text_private_channel_and_overrides_fall_back] = true
  listener, message = AgentBotListener.new([internal]), Message.new
  message.define_singleton_method(:webhook_sendable?) { false }
  listener.message_created(OpenStruct.new(data: { message: message }))
  checks[:false_receiver_gate_retains_native_no_event] = listener.jobs.empty? && message.serializations.zero?
  original_gate = MessageFilterHelpers.instance_method(:webhook_sendable?)
  MessageFilterHelpers.send(:define_method, :webhook_sendable?) { false }
  listener, message = AgentBotListener.new([internal]), Message.new
  listener.message_created(OpenStruct.new(data: { message: message }))
  checks[:false_class_gate_retains_native_no_event] = listener.jobs.empty? && message.serializations.zero?
  MessageFilterHelpers.send(:define_method, :webhook_sendable?, original_gate)
  [proc { |listener| listener.define_singleton_method(:message_created) { |event| super(event) } },
   proc { |listener| listener.define_singleton_method(:process_message_event) { |*args| super(*args) } },
   proc { |_| NevidimyAgentBotConversationProjection.instance_variable_set(:@valid, false) }].each do |change|
    listener, message = AgentBotListener.new([internal]), Message.new
    change.call(listener)
    listener.message_created(OpenStruct.new(data: { message: message }))
    verify(listener.jobs.first[:payload][:full_native] == true, 'unknown_contract_not_native')
    NevidimyAgentBotConversationProjection.instance_variable_set(:@valid, true)
  end
  checks[:unknown_listener_and_existing_projection_contract_fall_back] = true
  original = NevidimyAgentBotConversationProjection.method(:internal_recipient?)
  NevidimyAgentBotConversationProjection.define_singleton_method(:internal_recipient?) { |_| true }
  listener, message = AgentBotListener.new([external]), Message.new
  listener.message_created(OpenStruct.new(data: { message: message }))
  checks[:recipient_policy_override_falls_back] = listener.jobs.first[:payload][:full_native] == true
  NevidimyAgentBotConversationProjection.define_singleton_method(:internal_recipient?, original)
  message = Message.new
  message.message_type = 'outgoing'
  listener = AgentBotListener.new([internal])
  listener.message_created(OpenStruct.new(data: { message: message }))
  checks[:existing_self_filter_still_active] = listener.jobs.empty? && message.serializations.zero?
  message = Message.new
  message.fail_serialization = true
  listener = AgentBotListener.new([internal, external])
  begin
    listener.message_created(OpenStruct.new(data: { message: message }))
  rescue RuntimeError => error
    verify(error.message == 'neutral_serializer_failure', 'wrong_failure')
  end
  checks[:mixed_failure_enqueues_nothing] = listener.jobs.empty? && message.serializations == 1
  failure_jobs = [native, nil].map do |method|
    listener, message = AgentBotListener.new([internal, external]), Message.new
    message.fail_on_serialization = 2
    begin
      if method
        method.bind(listener).call(OpenStruct.new(data: { message: message }))
      else
        listener.message_created(OpenStruct.new(data: { message: message }))
      end
    rescue RuntimeError => error
      verify(error.message == 'neutral_serializer_failure', 'wrong_second_failure')
    end
    verify(message.serializations == 2 && listener.jobs.length == 1, 'mixed_second_failure_order')
    listener.jobs
  end
  checks[:mixed_second_failure_order_preserved] = contract_jobs(failure_jobs[0]) == contract_jobs(failure_jobs[1]) &&
    failure_jobs[0].first[:payload] == failure_jobs[1].first[:payload]
  checks[:visibility_preserved] = AgentBotListener.public_method_defined?(:message_created) &&
    AgentBotListener.private_method_defined?(:process_message_event)
  ENV[projection::OPT_IN] = '0'
  listener, message = AgentBotListener.new([internal]), Message.new
  listener.message_created(OpenStruct.new(data: { message: message }))
  checks[:runtime_opt_out] = listener.jobs.first[:payload][:full_native] == true
  verify(checks.values.all?, "neutral_contract_failed:#{checks.reject { |_, value| value }.keys.join(',')}")
  puts JSON.generate(ok: true, checks: checks, cases: cases, performed_jobs: 0,
                     scope: 'synthetic_structure_only_no_native_rails')
end

# Opt-in, internal-recipient-only projection of deterministic Telegram clicks.
# Keep native events, recipients, WebhookJob, signing and authoritative API reads.
require 'digest'

module NevidimyAgentBotMessageProjection
  OPT_IN = 'NEVIDIMY_AGENT_MESSAGE_PROJECTION'.freeze
  CALLBACK_ATTRIBUTE = 'nevidimy_telegram_callback_data'.freeze
  SOURCE_SHA256S = {
    'AgentBotListener' => 'cf733d9ade2e9ff4564e7f41deeeaede8e3a2a95edf7d4a5dbe0a6b4653088a3',
    'Message' => 'a4e604d536e6d39a75e393b906b77a833d4baccadd4bae2d6f0477017d497015',
    'MessageFilterHelpers' => '0c0b16c68061655aeae121f42951f20e840ad6a86f2f9b908bbbccd09ed4c2c0',
    'Contact' => '75ec0685efaa21b65f47a6845640bc5216bc44ba4441ccad494f156a8e312711',
    'Inbox' => '4575130acb38a15c49cacd346e99b52562e8ba2d4ed47feea3f61f2c6b0e7edb',
    'NevidimyTurnTiming::BotEvents' => '74ce222a55ccb0dbe715347b5a84cb92d70d890dd04b6b2e598d6036028d7cb8',
    'NevidimyAgentBotSelfEventFilter::Listener' => '0b1fd72768f49ffc9c3a239f404692f272724e2825b6ebd1a2698eb7df6efc9c',
    '#<Class:NevidimyAgentBotSelfEventFilter>' => '0b1fd72768f49ffc9c3a239f404692f272724e2825b6ebd1a2698eb7df6efc9c',
    '#<Class:NevidimyAgentBotConversationProjection>' => 'bd2d3770ce9aeba7497ac5a23f3a240c15e5735778acbaff3a723840268b4451'
  }.freeze

  def self.verified?(method)
    expected = SOURCE_SHA256S[method.owner.to_s]
    expected && method.source_location && Digest::SHA256.file(method.source_location.first).hexdigest == expected
  rescue StandardError
    false
  end

  def self.chain(method)
    methods = []
    while method
      methods << (method.is_a?(Method) ? method.unbind : method) unless method.owner == Listener
      method = method.super_method
    end
    methods
  end

  def self.install
    return false unless ENV[OPT_IN] == '1'
    return true if AgentBotListener < Listener
    return false unless File.read(Rails.root.join('VERSION_CW')).strip == '4.19.0' &&
                        Rails::VERSION::STRING == '7.2.3.1'
    return false unless defined?(NevidimyAgentBotConversationProjection::Listener) &&
                        AgentBotListener < NevidimyAgentBotConversationProjection::Listener &&
                        defined?(NevidimyAgentBotSelfEventFilter::Listener) &&
                        AgentBotListener < NevidimyAgentBotSelfEventFilter::Listener

    projection = NevidimyAgentBotConversationProjection
    return false unless %i[current_contract? internal_recipient?].all? { |name| verified?(projection.method(name)) } &&
                        verified?(NevidimyAgentBotSelfEventFilter::Listener.instance_method(:process_message_event)) &&
                        verified?(NevidimyAgentBotSelfEventFilter.method(:skip?))
    @projection_methods = %i[current_contract? internal_recipient?].to_h { |name| [name, projection.method(name)] }
    @self_filter_method = NevidimyAgentBotSelfEventFilter.method(:skip?)
    @listener_chain = chain(AgentBotListener.instance_method(:message_created))
    owners = @listener_chain.map { |method| method.owner.name }
    return false unless [['AgentBotListener'], ['NevidimyTurnTiming::BotEvents', 'AgentBotListener']].include?(owners) &&
                        @listener_chain.all? { |method| method.arity == 1 && verified?(method) }
    delivery = chain(AgentBotListener.instance_method(:process_message_event))
    return false unless delivery.map { |method| method.owner.name } ==
                        ['NevidimyAgentBotSelfEventFilter::Listener', 'AgentBotListener'] &&
                        delivery.all? { |method| method.arity == 4 && verified?(method) }

    specs = [[Message, :webhook_data, Message], [Message, :webhook_content, Message],
             [Message, :webhook_sendable?, MessageFilterHelpers], [Contact, :webhook_data, Contact], [Inbox, :webhook_data, Inbox]]
    return false unless specs.all? { |type, name, owner|
      method = type.instance_method(name)
      method.owner == owner && method.arity.zero? && verified?(method)
    }
    @serializers = specs.to_h { |type, name, _| [[type, name], type.instance_method(name)] }
    @timed = owners.first == 'NevidimyTurnTiming::BotEvents'
    AgentBotListener.prepend(Listener)
    # Ruby records the prepend origin in wrapper method identities.
    @listener_chain = chain(AgentBotListener.instance_method(:message_created))
    @message_event_chain = chain(AgentBotListener.instance_method(:process_message_event))
    @callback_method = AgentBotListener.instance_method(:nevidimy_project_callback)
    true
  rescue StandardError
    false
  end

  def self.same_chain?(listener, name, expected)
    actual = chain(listener.method(name))
    chain(AgentBotListener.instance_method(name)) == expected && actual.length == expected.length &&
      actual.zip(expected).all? { |current, original| same_method?(current, original) }
  end

  def self.same_method?(actual, original)
    actual.owner == original.owner && actual.original_name == original.original_name &&
      actual.arity == original.arity && actual.source_location == original.source_location
  end

  def self.same_dispatch?(actual, original)
    # A harmless singleton class changes Ruby's bound Method lookup origin.
    # Class identities remain pinned below; every receiver dispatch entry must
    # still match, so singleton definitions/prepends cannot hide an override.
    while actual && original
      return false unless same_method?(actual, original)

      actual = actual.super_method
      original = original.super_method
    end
    actual.nil? && original.nil?
  rescue StandardError
    false
  end

  def self.current_contract?(listener, message)
    return false unless @serializers && same_chain?(listener, :message_created, @listener_chain) &&
                        same_chain?(listener, :process_message_event, @message_event_chain) &&
                        AgentBotListener.instance_method(:nevidimy_project_callback) == @callback_method &&
                        same_dispatch?(listener.method(:nevidimy_project_callback), @callback_method)
    return false unless @serializers.all? { |(type, name), original| type.instance_method(name) == original }
    return false unless [[message, Message, :webhook_data], [message, Message, :webhook_content], [message, Message, :webhook_sendable?],
                         [message.sender, Contact, :webhook_data], [message.inbox, Inbox, :webhook_data]].all? { |receiver, type, name|
      receiver.instance_of?(type) && same_dispatch?(receiver.method(name), @serializers.fetch([type, name]))
    }
    return false unless @projection_methods.all? { |name, original|
      NevidimyAgentBotConversationProjection.method(name) == original
    }
    return false unless NevidimyAgentBotSelfEventFilter.method(:skip?) == @self_filter_method

    NevidimyAgentBotConversationProjection.current_contract?(listener, message.conversation)
  rescue StandardError
    false
  end

  def self.callback?(message)
    return false unless message.instance_of?(Message) && message.incoming? && message.private? == false &&
                        message.content_type == 'text' && message.conversation.instance_of?(Conversation) &&
                        message.inbox.instance_of?(Inbox) && message.inbox.channel_type == 'Channel::Telegram' &&
                        message.inbox_id == message.inbox.id && message.conversation.inbox_id == message.inbox.id &&
                        message.conversation.inbox.channel_type == 'Channel::Telegram' &&
                        message.sender.instance_of?(Contact)

    attributes = message.content_attributes
    callback = attributes[CALLBACK_ATTRIBUTE] if attributes.is_a?(Hash)
    callback.is_a?(String) && callback.valid_encoding? && !callback.strip.empty? && callback.bytesize <= 64
  rescue StandardError
    false
  end

  def self.payload(message)
    {
      event: 'message_created', id: message.id, message_type: message.message_type, private: message.private?,
      content: message.webhook_content, content_attributes: message.content_attributes,
      conversation: { id: message.conversation.display_id, channel: message.conversation.inbox.channel_type },
      sender: { id: message.sender.id }, inbox: { id: message.inbox.id }
    }
  end

  module Listener
    def message_created(event)
      return super unless ENV[NevidimyAgentBotMessageProjection::OPT_IN] == '1'

      message = extract_message_and_account(event)[0]
      return super unless NevidimyAgentBotMessageProjection.callback?(message) &&
                          NevidimyAgentBotMessageProjection.current_contract?(self, message)
      return unless message.webhook_sendable?
      bots = agent_bots_for(message.inbox, message.conversation)
      # Native message payloads are built per recipient, including their failure
      # boundaries. Do not change mixed/multiple-recipient fan-out at all.
      return super unless bots.length == 1 && NevidimyAgentBotConversationProjection.internal_recipient?(bots.first)

      # This prepend is installed after the observation wrapper. Preserve its
      # one native span when the verified native body below replaces super.
      if NevidimyAgentBotMessageProjection.instance_variable_get(:@timed)
        NevidimyTurnTiming.measure(:bot_message_created) { nevidimy_project_callback(message, bots.first) }
      else
        nevidimy_project_callback(message, bots.first)
      end
    end

    private

    def nevidimy_project_callback(message, bot)
      process_webhook_bot_event(bot, NevidimyAgentBotMessageProjection.payload(message))
      [bot]
    end
  end
end

# Do not hide the native owner before the existing self-event filter installs.
# Register reload preparation last for the same reason.
Rails.application.config.after_initialize do
  NevidimyAgentBotMessageProjection.install
  Rails.application.reloader.to_prepare { NevidimyAgentBotMessageProjection.install }
end

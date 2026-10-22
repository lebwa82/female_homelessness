# Avoid only marked self-created notifications ignored by the pinned consumer.
# All other target bots/events retain native serialization and WebhookJob handling.
require 'digest'

module NevidimyAgentBotSelfEventFilter
  LISTENER_SHA256 = 'cf733d9ade2e9ff4564e7f41deeeaede8e3a2a95edf7d4a5dbe0a6b4653088a3'.freeze unless const_defined?(:LISTENER_SHA256, false)
  AGENT_BOT_SHA256 = 'c1ac84ff6f5ef8de75c3375dd3ba5750b1c2775bda7a42c2b366930b8f6cdd32'.freeze unless const_defined?(:AGENT_BOT_SHA256, false)
  MESSAGE_BODY_SHA256 = 'a54d9455f7a77da6bc9e8e2de37c45a5db7a1809b12d2edad21fd2fd82186c3e'.freeze unless const_defined?(:MESSAGE_BODY_SHA256, false)

  def self.native_contract?(listener)
    native = listener.instance_method(:process_message_event)
    return false unless native.owner == listener && native.arity == 4 && native.source_location &&
                        Digest::SHA256.file(native.source_location.first).hexdigest == LISTENER_SHA256

    serializer = Message.instance_method(:webhook_data)
    return false unless serializer.owner == Message && serializer.arity.zero? && serializer.source_location

    path, line = serializer.source_location
    body = File.readlines(path)[line - 1, 19].join
    return false unless Digest::SHA256.hexdigest(body) == MESSAGE_BODY_SHA256

    sender = AgentBot.instance_method(:webhook_data)
    sender.owner == AgentBot && sender.arity.zero? && sender.source_location &&
      Digest::SHA256.file(sender.source_location.first).hexdigest == AGENT_BOT_SHA256
  rescue StandardError
    false
  end

  def self.skip?(method_name, agent_bot, message)
    return false unless method_name == 'message_created' && message.outgoing? && message.sender_type == 'AgentBot'
    return false unless message.sender_id.is_a?(Integer) && message.sender_id.positive? &&
                        agent_bot.id.is_a?(Integer) && agent_bot.id.positive? && message.sender_id == agent_bot.id
    return false unless agent_bot.instance_of?(AgentBot)

    approved = ENV['NEVIDIMY_AGENT_WEBHOOK_URL']
    return false unless approved.is_a?(String) &&
                        approved.match?(%r{\Ahttps://agent-bot:8080/webhooks/chatwoot/agent/[A-Za-z0-9_-]{32,128}\z}) &&
                        agent_bot.outgoing_url.is_a?(String) && agent_bot.outgoing_url == approved

    attributes = message.content_attributes
    return false unless attributes.is_a?(Hash)

    marked = if message.private? == false
      key = attributes['bot_turn_key']
      key.is_a?(String) && key.match?(/\Amessage:[1-9][0-9]{0,18}\z/)
    elsif message.private? == true
      key = attributes['bot_event_key']
      key.is_a?(String) && key.match?(/\Ascenario(?:-alert)?:[0-9a-f]{24}\z/)
    else
      false
    end
    return false unless marked && message.method(:webhook_data).unbind == @message_serializer

    # Verify the serializer actually used by sender.try(:webhook_data), without
    # executing it or serializing a tree. Unknown/singleton overrides stay native.
    sender = message.sender
    sender.instance_of?(AgentBot) && sender.id == message.sender_id &&
      sender.method(:webhook_data).unbind == @sender_serializer
  rescue StandardError
    # Unrecognized configuration/metadata is never proof that an event is unused.
    false
  end

  def self.install(listener)
    return if listener < Listener
    return unless native_contract?(listener)

    @message_serializer = Message.instance_method(:webhook_data)
    @sender_serializer = AgentBot.instance_method(:webhook_data)
    listener.prepend(Listener)
  end

  module Listener
    private

    def process_message_event(method_name, agent_bot, message, event)
      return if NevidimyAgentBotSelfEventFilter.skip?(method_name, agent_bot, message)

      super
    end
  end
end

Rails.application.reloader.to_prepare do
  next unless defined?(AgentBotListener) && defined?(AgentBot) && defined?(Message)
  NevidimyAgentBotSelfEventFilter.install(AgentBotListener)
end

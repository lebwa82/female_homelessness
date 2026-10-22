# The internal consumer re-reads conversations; retain its event/delta, not an
# unused conversation tree. Other recipients keep the complete native payload.
require 'digest'

module NevidimyAgentBotConversationProjection
  OPT_IN = 'NEVIDIMY_AGENT_CONVERSATION_PROJECTION'.freeze
  BACKGROUND_WAITING = 'NEVIDIMY_AGENT_BACKGROUND_WAITING'.freeze
  OWNER_SHA256S = {
    'AgentBotListener' => 'cf733d9ade2e9ff4564e7f41deeeaede8e3a2a95edf7d4a5dbe0a6b4653088a3',
    'BaseListener' => '50db9ab0c06244e023d58b4b7dd01d9b9e2c4fa37745f535500ffa6af162bc76',
    'PushDataHelper' => 'e1544ce45432d7e540d6df5f4594f422e184689ebcfcd93434e6b828fcccd399',
    'Conversations::EventDataPresenter' => '3e985c30c4ec6741f9dd97e19e360205f4a027ab65c88935f26d43e7d65926d2',
    'Enterprise::Conversations::EventDataPresenter' => '229f4765111a716df7a168cce7ef940190e54c4e61deca85b41dee970e584ece',
    'AgentBot' => 'c1ac84ff6f5ef8de75c3375dd3ba5750b1c2775bda7a42c2b366930b8f6cdd32'
  }.freeze
  WRAPPER_SHA256S = {
    'NevidimyTurnTiming::BotEvents' => '2ec22c547e16b9e435e77c2dd11875a4f682614f71c1ef4f242c3946ef401c3c',
    'NevidimyWebhookPresenterFastPath::Presenter' => '91992962f5e852d0b088000a817517ffd5b9185ba3b5761e5d7ab4fd3d29b18a'
  }.freeze
  LISTENER_METHODS = {
    conversation_updated: 1, agent_bots_for: -2, active_inbox_agent_bot: 1,
    process_webhook_bot_event: 2, extract_conversation_and_account: 1, extract_changed_attributes: 1
  }.freeze
  PRESENTER_METHODS = %i[webhook_data push_data push_messages webhook_push_messages].freeze

  def self.wrapper?(method)
    owner = method.owner
    return true if owner == Listener && method.name == :conversation_updated

    @wrappers && @wrappers.any? { |original|
      owner == original.owner && method.name == original.name &&
        owner.instance_method(method.name) == original && method.source_location == original.source_location &&
        method.arity == original.arity
    }
  end

  def self.chain(method)
    methods = []
    while method
      methods << (method.is_a?(Method) ? method.unbind : method) unless wrapper?(method)
      method = method.super_method
    end
    methods
  end

  def self.method_specs
    LISTENER_METHODS.map { |name, arity| [AgentBotListener, name, arity] } +
      [[Conversation, :webhook_data, 0], [AgentBot, :webhook_data, 0]] +
      PRESENTER_METHODS.map { |name| [Conversations::EventDataPresenter, name, 0] }
  end

  def self.verified_contract
    return unless File.read(Rails.root.join('VERSION_CW')).strip == '4.19.0'

    method_specs.to_h do |type, name, arity|
      actual = chain(type.instance_method(name))
      return unless !actual.empty? && actual.all? { |method|
        expected = OWNER_SHA256S[method.owner.name]
        method.arity == arity && expected && method.source_location &&
          Digest::SHA256.file(method.source_location.first).hexdigest == expected
      }

      [[type, name], actual]
    end
  rescue StandardError
    nil
  end

  def self.current_contract?(listener, conversation)
    return false unless @contract
    return false unless @contract.all? { |(type, name), expected| chain(type.instance_method(name)) == expected }
    return false unless LISTENER_METHODS.all? { |name, _|
      same_receiver_chain?(listener, AgentBotListener, name)
    }

    same_receiver_chain?(conversation, Conversation, :webhook_data)
  rescue StandardError
    false
  end

  def self.same_receiver_chain?(receiver, type, name)
    actual = chain(receiver.method(name))
    expected = @contract.fetch([type, name])
    actual.length == expected.length && actual.zip(expected).all? { |current, original|
      current.bind(receiver) == original.bind(receiver)
    }
  end

  def self.internal_recipient?(bot)
    approved = ENV['NEVIDIMY_AGENT_WEBHOOK_URL']
    bot.instance_of?(AgentBot) && bot.id.is_a?(Integer) && bot.id.positive? &&
      approved.is_a?(String) && approved.match?(%r{\Ahttps://agent-bot:8080/webhooks/chatwoot/agent/[A-Za-z0-9_-]{32,128}\z}) &&
      bot.method(:outgoing_url).owner.name == 'AgentBot::GeneratedAttributeMethods' && bot.outgoing_url == approved
  rescue StandardError
    false
  end

  def self.waiting_only?(changes)
    return false unless changes.is_a?(Array) && changes.length == 2

    fields = {}
    changes.each do |item|
      return false unless item.is_a?(Hash) && item.length == 1
      name, delta = item.first
      return false unless name.is_a?(String) || name.is_a?(Symbol)
      name = name.to_s
      return false unless %w[waiting_since updated_at].include?(name) && !fields.key?(name)
      return false unless delta.is_a?(Hash) && delta.keys.all? { |key| key.is_a?(Symbol) } &&
                          delta.keys.sort == %i[current_value previous_value]

      fields[name] = [delta[:previous_value], delta[:current_value]]
    end
    return false unless fields.keys.sort == %w[updated_at waiting_since]

    timestamp = ->(value) { value.is_a?(Time) || (defined?(ActiveSupport::TimeWithZone) && value.is_a?(ActiveSupport::TimeWithZone)) }
    before, after = fields['waiting_since']
    fields['updated_at'].all?(&timestamp) &&
      ((before.nil? && timestamp.call(after)) || (after.nil? && timestamp.call(before)))
  end

  def self.install
    @wrappers = []
    [[defined?(NevidimyTurnTiming::BotEvents) && NevidimyTurnTiming::BotEvents, [:conversation_updated]],
     [defined?(NevidimyWebhookPresenterFastPath::Presenter) && NevidimyWebhookPresenterFastPath::Presenter,
      %i[webhook_data push_messages]]].each do |owner, names|
      next unless owner

      names.each do |name|
        method = owner.instance_method(name)
        expected = WRAPPER_SHA256S[owner.name]
        @wrappers << method if expected && method.source_location &&
                               Digest::SHA256.file(method.source_location.first).hexdigest == expected
      end
    end
    @contract = verified_contract
    return unless @contract

    AgentBotListener.prepend(Listener) unless AgentBotListener < Listener
  rescue StandardError
    @contract = nil
  end

  module Listener
    def conversation_updated(event)
      return super unless ENV[NevidimyAgentBotConversationProjection::OPT_IN] == '1'

      conversation = extract_conversation_and_account(event)[0]
      return super unless conversation.instance_of?(Conversation) && conversation.display_id.is_a?(Integer) &&
                          conversation.display_id.positive? &&
                          NevidimyAgentBotConversationProjection.current_contract?(self, conversation)

      changed_attributes = extract_changed_attributes(event)
      bots = agent_bots_for(conversation.inbox, conversation)
      return super if bots.empty?

      internal = bots.map { |bot| NevidimyAgentBotConversationProjection.internal_recipient?(bot) }
      # Native serialization precedes every enqueue. Preserve that failure
      # boundary whenever any recipient still needs the complete payload.
      full_payload = conversation.webhook_data.merge(event: 'conversation_updated', changed_attributes: changed_attributes) unless internal.all?
      bots.zip(internal).each do |bot, compact|
        payload = if compact
          { id: conversation.display_id, event: 'conversation_updated', changed_attributes: changed_attributes }
        else
          full_payload
        end
        if compact && ENV[NevidimyAgentBotConversationProjection::BACKGROUND_WAITING] == '1' &&
           NevidimyAgentBotConversationProjection.waiting_only?(changed_attributes)
          # Keep the same durable native job, delivery identity and retry policy.
          # Only timer bookkeeping moves off the interactive queue; ownership,
          # unknown/mixed deltas and other recipients retain native priority.
          AgentBots::WebhookJob.set(queue: :medium).perform_later(
            bot.outgoing_url, payload, :agent_bot_webhook,
            secret: bot.secret, delivery_id: SecureRandom.uuid
          )
        else
          process_webhook_bot_event(bot, payload)
        end
      end
    end
  end
end

Rails.application.reloader.to_prepare do
  NevidimyAgentBotConversationProjection.install
end

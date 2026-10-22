# Compact projection/conditional commit on existing, authenticated API actions.
# No new route, token, public port or scenario engine. Native message creation
# and delivery remain authoritative; internal workflow writes emit no redundant
# conversation_updated webhooks. Business effects use the established handler.
require 'json'
require 'time'

module NevidimyAtomicTurn
  CAPABILITY = 'nevidimy-atomic-turn-v1'.freeze
  ATTRIBUTES = %w[workflow_state workflow_need pending_aid_id pending_contact_method pending_city
                  pending_district pending_offer context_epoch workflow_navigation scenario
                  scenario_pending_input scenario_last_message_id scenario_followups consultation_reviewed].freeze

  def self.version(conversation)
    conversation.updated_at.utc.iso8601(6)
  end

  def self.input(conversation, id)
    return unless id.to_s.match?(/\A[1-9][0-9]{0,18}\z/)

    conversation.messages.find_by(id: id.to_i, message_type: :incoming)
  end

  def self.reply(conversation, input_id)
    # Native Chatwoot uses `store ... coder: JSON`: on the installed version
    # the jsonb value is a JSON *string*, not an object. Read through the model
    # coder instead of querying a JSON key that silently misses that encoding.
    # Only outgoing rows newer than this input can belong to its turn; do not
    # reload the conversation history or depend on a particular DB encoding.
    conversation.messages.where(message_type: :outgoing, private: false)
                .where('id > ?', input_id).select(:id, :content_attributes).detect do |message|
      attributes = message.content_attributes
      attributes.is_a?(Hash) && attributes['bot_turn_key'] == "message:#{input_id}"
    end&.id
  end

  def self.eligible?(conversation, input_id)
    attributes = conversation.custom_attributes || {}
    return false unless %w[open pending].include?(conversation.status) && conversation.assignee_id.nil?
    return false unless attributes['reply_owner'] == 'bot' && attributes['ownership_version'] == 3
    return false if attributes['scenario_pending_input'].present?
    watermarks = %w[scenario_last_message_id last_clear_message_id ownership_last_staff_message_id]
    return false if watermarks.any? { |key| attributes[key].to_i > input_id }

    !conversation.messages.where(message_type: :outgoing, private: false, sender_type: 'User')
                 .where('id > ?', input_id).exists?
  end

  module Projection
    def show
      return super unless params.key?(:nevidimy_turn_state)

      incoming = NevidimyAtomicTurn.input(@conversation, params[:nevidimy_turn_state])
      return head :not_found unless incoming

      render json: {
        capability: CAPABILITY, input_id: incoming.id,
        version: NevidimyAtomicTurn.version(@conversation),
        eligible: NevidimyAtomicTurn.eligible?(@conversation, incoming.id),
        reply_id: NevidimyAtomicTurn.reply(@conversation, incoming.id),
        conversation: {
          id: @conversation.display_id, status: @conversation.status,
          custom_attributes: @conversation.custom_attributes,
          assignee_id: @conversation.assignee_id, team_id: @conversation.team_id,
          meta: { assignee: @conversation.assignee_id && { id: @conversation.assignee_id },
                  assignee_type: @conversation.assignee_id ? 'User' : 'AgentBot',
                  team: @conversation.team_id && { id: @conversation.team_id } }
        }
      }
    end
  end

  module Commit
    def create
      return super unless params.key?(:nevidimy_atomic_turn)
      # Keep the native account/conversation policy and require the actual bound
      # Agent Bot, not the read identity or an arbitrary authenticated user.
      return head :forbidden unless Current.user.is_a?(AgentBot) &&
                                    AgentBotInbox.exists?(inbox_id: @conversation.inbox_id, agent_bot_id: Current.user.id)

      turn = params[:nevidimy_atomic_turn]
      # This is a proven no-write unsupported plan, not an unknown POST outcome.
      # Allow the client to use the established handler even when Rails and
      # Python serialize large/HTML-containing JSON with different byte counts.
      return head :conflict unless valid_nevidimy_turn?(turn)

      result = @conversation.with_lock do
        incoming = NevidimyAtomicTurn.input(@conversation, turn[:input_id])
        next nil unless incoming
        duplicate = NevidimyAtomicTurn.reply(@conversation, incoming.id)
        next { id: duplicate, outcome: 'duplicate' } if duplicate
        next nil unless NevidimyAtomicTurn.version(@conversation) == turn[:version] &&
                        NevidimyAtomicTurn.eligible?(@conversation, incoming.id)
        next nil if turn[:attributes].key?('context_epoch') &&
                    turn[:attributes]['context_epoch'] != (@conversation.custom_attributes['context_epoch'] || 0)

        # Only the explicit internal workflow allowlist can bypass callbacks.
        # Native assignment/status, durable business requests and human notes
        # cannot enter this path. Merge under lock preserves unrelated fields.
        changes = turn[:attributes].to_unsafe_h
        @conversation.update_columns(custom_attributes: @conversation.custom_attributes.merge(changes),
                                     updated_at: Time.current)
        payload = params.permit(:content, :content_type, content_attributes: {})
        payload[:message_type] = 'outgoing'
        payload[:private] = false
        payload[:content_attributes] ||= {}
        payload[:content_attributes][:bot_turn_key] = "message:#{incoming.id}"
        payload[:content_attributes].delete(:nevidimy_received_at_ms)
        received_at = incoming.content_attributes['nevidimy_received_at_ms']
        if received_at.is_a?(Integer) && received_at.positive? && received_at <= (Time.now.to_f * 1000).to_i
          payload[:content_attributes][:nevidimy_received_at_ms] = received_at
        end
        @message = Messages::MessageBuilder.new(Current.user, @conversation, payload).perform
        { id: @message.id, outcome: 'committed' }
      end
      return head :conflict unless result

      render json: result
    end

    private

    def valid_nevidimy_turn?(turn)
      return false unless turn.is_a?(ActionController::Parameters) &&
                          turn.keys.sort == %w[attributes input_id version]
      changes = turn[:attributes]
      return false unless changes.is_a?(ActionController::Parameters) && (changes.keys - ATTRIBUTES).empty?
      return false unless turn[:version].is_a?(String) && turn[:version].bytesize <= 40
      return false unless params[:content].is_a?(String) && params[:content].bytesize <= 32_768
      return false if params[:attachments].present?
      return false unless [nil, 'text', 'input_select'].include?(params[:content_type])

      # No reset/epoch advancement or synthetic out-of-order watermark through
      # a speculative button plan. These commands retain the original path.
      incoming_id = turn[:input_id].to_s
      return false unless incoming_id.match?(/\A[1-9][0-9]{0,18}\z/)
      return false if changes['scenario_pending_input'].present?
      return false unless changes['scenario_last_message_id'] == incoming_id.to_i

      changes.to_unsafe_h.to_json.bytesize <= 262_144
    end
  end

  module StaffSerialization
    private

    def nevidimy_lock_staff_conversation
      return unless outgoing? && !private? && sender_type == 'User'

      # Native staff writes participate in the same conversation lock as the
      # conditional bot commit, including replies whose webhook is still queued.
      Conversation.where(id: conversation_id).lock(true).pick(:id)
    end
  end
end

Rails.application.reloader.to_prepare do
  conversations = Api::V1::Accounts::ConversationsController
  messages = Api::V1::Accounts::Conversations::MessagesController
  conversations.prepend(NevidimyAtomicTurn::Projection) unless conversations < NevidimyAtomicTurn::Projection
  messages.prepend(NevidimyAtomicTurn::Commit) unless messages < NevidimyAtomicTurn::Commit
  unless Message < NevidimyAtomicTurn::StaffSerialization
    Message.include(NevidimyAtomicTurn::StaffSerialization)
    Message.before_create :nevidimy_lock_staff_conversation
  end
end

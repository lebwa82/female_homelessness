# Narrow, user-approved exception for the signed Docker-internal Agent Bot only.
# Keep SafeFetch's global private-network protection enabled for every other URL.
require 'net/http'
require 'uri'
require 'openssl'

module NevidimyInternalAgentWebhook
  private

  def perform_request
    expected = ENV['NEVIDIMY_AGENT_WEBHOOK_URL']
    return super unless @webhook_type == :agent_bot_webhook && expected == @url &&
                        expected&.match?(%r{\Ahttps?://agent-bot:8080/webhooks/chatwoot/agent/[A-Za-z0-9_-]{32,128}\z})

    unless @secret.is_a?(String) && !@secret.empty?
      raise SafeFetch::UnsafeUrlError, 'internal Agent Bot requires a signing secret'
    end

    uri = URI(expected)
    body = nevidimy_snapshot_body(@payload.to_json)
    request = Net::HTTP::Post.new(uri.request_uri, request_headers(body))
    request.body = body
    # Explicit nil disables environment proxies: this traffic must stay in Docker.
    http = Net::HTTP.new(uri.hostname, uri.port, nil)
    if uri.scheme == 'https'
      http.use_ssl = true
      http.verify_mode = OpenSSL::SSL::VERIFY_PEER
      http.ca_file = ENV.fetch('SSL_CERT_FILE')
      http.min_version = OpenSSL::SSL::TLS1_2_VERSION
    end
    http.open_timeout = webhook_timeout
    http.read_timeout = webhook_timeout
    http.write_timeout = webhook_timeout
    http.max_retries = 0
    http.start do |connection|
      connection.request(request) do |response|
        # In particular, never follow a redirect into another private endpoint.
        unless response.is_a?(Net::HTTPSuccess)
          raise SafeFetch::HttpError, "#{response.code} #{response.message}"
        end

        bytes = 0
        response.read_body do |chunk|
          bytes += chunk.bytesize
          raise SafeFetch::FileTooLargeError, 'webhook response exceeds 64 KiB' if bytes > 65_536
        end
      end
    end
  end

  # Read at delivery, not in the synchronous MESSAGE_CREATED listener:
  # waiting_since can advance updated_at after dispatch. If this job races the
  # remaining callbacks, the authoritative POST still rejects a stale version.
  # The original queued payload/arguments remain untouched on every attempt.
  def nevidimy_snapshot_body(original_body)
    return original_body unless ENV['NEVIDIMY_AGENT_BUTTON_SNAPSHOT'] == '1' && defined?(NevidimyAtomicTurn)

    payload = JSON.parse(original_body)
    return original_body unless payload.is_a?(Hash) && payload['event'] == 'message_created' &&
                                payload['message_type'] == 'incoming' && payload['private'] == false &&
                                payload.dig('conversation', 'channel') == 'Channel::Telegram'

    input_id, contact_id, inbox_id, display_id = payload['id'], payload.dig('sender', 'id'),
                                               payload.dig('inbox', 'id'), payload.dig('conversation', 'id')
    return original_body unless [input_id, contact_id, inbox_id, display_id].all? { |id| id.is_a?(Integer) && id.positive? }

    callback = payload.dig('content_attributes', 'nevidimy_telegram_callback_data')
    return original_body unless callback.is_a?(String) && callback.valid_encoding? &&
                                !callback.strip.empty? && callback.bytesize <= 64

    input = Message.uncached { Message.find_by(id: input_id) }
    return original_body unless input.instance_of?(Message) && input.incoming? && input.private? == false &&
                                input.sender_type == 'Contact' && input.sender_id == contact_id && input.inbox_id == inbox_id &&
                                input.content_attributes.is_a?(Hash) &&
                                input.content_attributes['nevidimy_telegram_callback_data'] == callback

    # One fresh row provides a coherent version AND every planning field.
    conversation = Conversation.uncached { Conversation.find_by(id: input.conversation_id) }
    return original_body unless conversation.instance_of?(Conversation) && conversation.display_id == display_id &&
                                conversation.account_id == input.account_id && conversation.inbox_id == inbox_id &&
                                conversation.contact_id == contact_id && conversation.custom_attributes.is_a?(Hash)

    snapshot = {
      schema: 1, capability: NevidimyAtomicTurn::CAPABILITY, features: ['private_notes_v1'],
      input_id: input_id, contact_id: contact_id, inbox_id: inbox_id,
      version: NevidimyAtomicTurn.version(conversation),
      conversation: {
        id: display_id, status: conversation.status, custom_attributes: conversation.custom_attributes,
        assignee_id: conversation.assignee_id, team_id: conversation.team_id,
        meta: { assignee: conversation.assignee_id && { id: conversation.assignee_id },
                assignee_type: conversation.assignee_id ? 'User' : 'AgentBot',
                team: conversation.team_id && { id: conversation.team_id } }
      }
    }
    return original_body if snapshot.to_json.bytesize > 262_144

    body = payload.merge('nevidimy_button_snapshot' => snapshot).to_json
    # aiohttp's existing 1 MiB limit applies to the WHOLE signed body.
    body.bytesize < 1_048_576 ? body : original_body
  rescue StandardError
    original_body # Optional optimization failure must never drop the input.
  end
end

Rails.application.reloader.to_prepare do
  Webhooks::Trigger.prepend(NevidimyInternalAgentWebhook) unless Webhooks::Trigger < NevidimyInternalAgentWebhook
end

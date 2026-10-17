# Narrow, user-approved exception for the signed Docker-internal Agent Bot only.
# Keep SafeFetch's global private-network protection enabled for every other URL.
require 'net/http'
require 'uri'

module NevidimyInternalAgentWebhook
  private

  def perform_request
    expected = ENV['NEVIDIMY_AGENT_WEBHOOK_URL']
    return super unless @webhook_type == :agent_bot_webhook && expected == @url &&
                        expected&.match?(%r{\Ahttp://agent-bot:8080/webhooks/chatwoot/agent/[A-Za-z0-9_-]{32,128}\z})

    unless @secret.is_a?(String) && !@secret.empty?
      raise SafeFetch::UnsafeUrlError, 'internal Agent Bot requires a signing secret'
    end

    uri = URI(expected)
    body = @payload.to_json
    request = Net::HTTP::Post.new(uri.request_uri, request_headers(body))
    request.body = body
    # Explicit nil disables environment proxies: this traffic must stay in Docker.
    http = Net::HTTP.new(uri.hostname, uri.port, nil)
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
end

Rails.application.reloader.to_prepare do
  Webhooks::Trigger.prepend(NevidimyInternalAgentWebhook) unless Webhooks::Trigger < NevidimyInternalAgentWebhook
end

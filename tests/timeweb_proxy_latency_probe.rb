# Standalone read-only Telegram getMe diagnostic. Never load Rails or the app.
# The launcher owns the pinned image/cgroup and supplies only TOKEN and PROXY.
require 'json'
require 'net/http'
require 'openssl'

class NevidimyProxyLatencyProbe
  class Rejected < StandardError; end

  EXPECTED_BOT_ID = 8831692160
  EXPECTED_PROXY = 'http://telegram-proxy:3128'.freeze
  SAFE_ERRORS = %w[
    NevidimyProxyLatencyProbe::Rejected KeyError ArgumentError SocketError
    EOFError IOError Errno::ECONNREFUSED Errno::ECONNRESET Errno::ETIMEDOUT
    Errno::EPIPE Net::OpenTimeout Net::ReadTimeout Net::WriteTimeout
    OpenSSL::SSL::SSLError JSON::ParserError Timeout::Error
  ].freeze

  # Net::HTTP may reconnect an expired/closed keepalive even with retries off.
  # Refuse a second connect rather than label a new TLS session as reused.
  class SingleConnectionHTTP < Net::HTTP
    private

    def connect
      @connect_count = @connect_count.to_i + 1
      raise Rejected unless @connect_count == 1

      super
    end
  end

  def self.error_type(error)
    name = error.class.name
    SAFE_ERRORS.include?(name) ? name : 'OtherError'
  end

  def monotonic
    Process.clock_gettime(Process::CLOCK_MONOTONIC)
  end

  def run(token:, proxy:)
    report = { ok: false, connect_ms: nil, samples: [] }
    http = nil
    begin
      raise Rejected unless proxy == EXPECTED_PROXY && token.is_a?(String) &&
                            token.match?(/\A8831692160:[A-Za-z0-9_-]{20,128}\z/)

      # Explicit proxy and nil credentials disable all ambient proxy selection.
      http = SingleConnectionHTTP.new('api.telegram.org', 443, 'telegram-proxy', 3128, nil, nil)
      http.use_ssl = true
      http.verify_mode = OpenSSL::SSL::VERIFY_PEER
      http.verify_hostname = true
      http.min_version = OpenSSL::SSL::TLS1_2_VERSION
      http.max_retries = 0
      http.open_timeout = http.read_timeout = http.write_timeout = 10
      http.keep_alive_timeout = 60
      started = monotonic
      http.start
      report[:connect_ms] = ((monotonic - started) * 1000).round(3)
      20.times do |index|
        body = index.even? ? '' : '{}'
        sample = { body_mode: index.even? ? 'empty' : 'json_object', elapsed_ms: nil, identity_ok: false }
        request = Net::HTTP::Post.new("/bot#{token}/getMe")
        request['Content-Type'] = 'application/json'
        request.body = body
        started = monotonic
        begin
          http.request(request) do |response|
            # No redirect/retry/fallback, and no response content in the report.
            raise Rejected unless response.is_a?(Net::HTTPOK)

            raw = +''
            response.read_body do |chunk|
              raw << chunk
              raise Rejected if raw.bytesize > 65_536
            end
            payload = JSON.parse(raw)
            identity = payload.is_a?(Hash) && payload['ok'] == true && payload['result'].is_a?(Hash) &&
                       payload['result']['id'].is_a?(Integer) && payload['result']['id'] == EXPECTED_BOT_ID &&
                       payload['result']['is_bot'] == true
            sample[:identity_ok] = identity == true
            raise Rejected unless sample[:identity_ok]
          end
        ensure
          sample[:elapsed_ms] = ((monotonic - started) * 1000).round(3)
          report[:samples] << sample
        end
      end
      report[:ok] = true
    rescue StandardError => error
      report[:error_type] = self.class.error_type(error)
    ensure
      begin
        http.finish if http && http.started?
      rescue StandardError => error
        report[:ok] = false
        report[:error_type] = self.class.error_type(error)
      end
    end
    report
  end
end

if $PROGRAM_NAME == __FILE__
  begin
    report = NevidimyProxyLatencyProbe.new.run(token: ENV.fetch('TOKEN'), proxy: ENV.fetch('PROXY'))
  rescue StandardError => error
    report = { ok: false, error_type: NevidimyProxyLatencyProbe.error_type(error) }
  end
  puts "PROXY_LATENCY_PROBE=#{JSON.generate(report)}"
  exit(report[:ok] ? 0 : 1)
end

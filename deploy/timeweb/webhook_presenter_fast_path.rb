# The pinned native webhook_data overwrites push_data[:messages]. Avoid only
# that discarded tree; retain native payload construction, including EE SLA data.
require 'digest'

module NevidimyWebhookPresenterFastPath
  SCOPE_KEY = :nevidimy_webhook_presenter_fast_path unless const_defined?(:SCOPE_KEY, false)
  BASE_SHA256 = '3e985c30c4ec6741f9dd97e19e360205f4a027ab65c88935f26d43e7d65926d2'.freeze unless const_defined?(:BASE_SHA256, false)
  EE_SHA256 = '229f4765111a716df7a168cce7ef940190e54c4e61deca85b41dee970e584ece'.freeze unless const_defined?(:EE_SHA256, false)

  def self.native_contract?(presenter)
    methods = %i[webhook_data push_messages webhook_push_messages].map { |name| presenter.instance_method(name) }
    return false unless methods.all? { |method| method.owner == presenter && method.arity.zero? }

    push_data = presenter.instance_method(:push_data)
    if push_data.owner != presenter
      return false unless push_data.owner.name == 'Enterprise::Conversations::EventDataPresenter' &&
                          push_data.arity.zero? && push_data.source_location &&
                          Digest::SHA256.file(push_data.source_location.first).hexdigest == EE_SHA256

      push_data = push_data.super_method
    end
    return false unless push_data && push_data.owner == presenter && push_data.arity.zero?

    locations = (methods + [push_data]).map(&:source_location)
    return false unless locations.all? && locations.map(&:first).uniq.size == 1

    Digest::SHA256.file(locations.first.first).hexdigest == BASE_SHA256
  rescue StandardError
    # Missing source or an unverified override disables this optional optimization.
    false
  end

  module Presenter
    def webhook_data
      previous = Thread.current[SCOPE_KEY]
      # Thread#[] is fiber-local; identity binds this one-shot flag to this exact
      # presenter without mutating it or sharing scope with another request.
      Thread.current[SCOPE_KEY] = { presenter: self, pending: true }
      begin
        super
      ensure
        Thread.current[SCOPE_KEY] = previous
      end
    end

    private

    def push_messages
      scope = Thread.current[SCOPE_KEY]
      if scope.is_a?(Hash) && scope[:presenter].equal?(self) && scope[:pending]
        scope[:pending] = false
        return []
      end

      super
    end
  end
end

Rails.application.reloader.to_prepare do
  next unless defined?(Conversations::EventDataPresenter)

  presenter = Conversations::EventDataPresenter
  next if presenter < NevidimyWebhookPresenterFastPath::Presenter
  next unless NevidimyWebhookPresenterFastPath.native_contract?(presenter)

  presenter.prepend(NevidimyWebhookPresenterFastPath::Presenter)
end

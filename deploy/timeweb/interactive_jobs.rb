# Reserve one of the existing two workers for delivery/webhooks. Queue names stay
# native so rollback can drain the same durable Redis jobs without migration.
require 'digest'
require 'json'

module NevidimyInteractiveJobs
  CAPSULE = 'nevidimy_interactive'.freeze
  SEND_REPLY_SHA256S = %w[
    8beaf0b6ff088e0c73e062f7c5b184c242ca00077c149d02cd4e19a317c7701e
    aa6a3eee8d0dd193f3903614a245d535633d84acaf0064e28ca57c7f91db61e6
  ].freeze
  BASE_SERVICE_SHA256 = 'f4006490ac594a59c6fccb4bd6e0cad89305fec4d5974a802e2c6ee8a8290258'.freeze
  TELEGRAM_SERVICE_SHA256 = '52eec4832b08826f5faa9bc1349ec3eb34ec19720c6fed4b67a37d279d053402'.freeze
  MESSAGE_BODY_SHA256 = '15e02ef2db60b84e6a18b2d6f608cc41aa895615959df760260359f809086d53'.freeze

  def self.isolate(config)
    return false unless Sidekiq::VERSION == '7.3.10' && config.capsules.keys == ['default']

    background = config.default_capsule
    queues = background.queues.dup
    return false unless config.total_concurrency == 2 && background.mode == :strict &&
                        queues.count('high') == 1 && queues.length > 1

    config.capsule(CAPSULE) do |interactive|
      interactive.queues = ['high']
      interactive.concurrency = 1
    end
    background.queues = queues.reject { |name| name == 'high' }
    background.concurrency = 1
    true
  end

  def self.native_file?(type, name, digests)
    method = type.instance_method(name)
    method.owner == type && method.source_location &&
      Array(digests).include?(Digest::SHA256.file(method.source_location.first).hexdigest)
  end

  def self.native_delivery?
    return false unless native_file?(SendReplyJob, :perform, SEND_REPLY_SHA256S) &&
                        native_file?(Base::SendOnChannelService, :perform, BASE_SERVICE_SHA256) &&
                        native_file?(Telegram::SendOnTelegramService, :perform_reply, TELEGRAM_SERVICE_SHA256)

    method = Message.instance_method(:send_reply)
    # Our timing wrapper only yields and records timings. Unknown overrides must
    # retain native enqueue behavior rather than silently losing their effects.
    method = method.super_method if method.owner.name == 'NevidimyTurnTiming::MessageCallbacks'
    return false unless method && method.owner == Message && method.arity.zero? && method.source_location

    path, line = method.source_location
    Digest::SHA256.hexdigest(File.readlines(path)[line - 1, 5].join) == MESSAGE_BODY_SHA256
  rescue StandardError
    false
  end

  def self.undeliverable_telegram?(message)
    return false unless message.incoming? || message.private?

    attributes = message.content_attributes
    # Chatwoot 4.19 runs translation before channel delivery. Ruby treats every
    # value except nil/false as pending, including 0, empty strings and containers.
    return false unless attributes.is_a?(Hash) && !attributes['translation_pending']

    conversation = message.conversation
    return false unless message.inbox_id.is_a?(Integer) && message.inbox_id.positive? &&
                        message.inbox_id == conversation.inbox_id

    conversation.inbox.channel.instance_of?(Channel::Telegram)
  rescue StandardError
    false
  end

  def self.install
    return if Message < MessageDelivery
    return unless native_delivery?

    Message.prepend(MessageDelivery)
  end

  module MessageDelivery
    private

    def send_reply
      # Pinned Telegram delivery returns without any write/send for these rows
      # only when no translation is pending. Keep their rows, callbacks and UI events;
      # omit only that proven no-op job, not public replies or other channels.
      return if NevidimyInteractiveJobs.undeliverable_telegram?(self)

      super
    end
  end
end

Sidekiq.configure_server do |config|
  isolated = NevidimyInteractiveJobs.isolate(config)
  config.on(:startup) do
    config.logger.info(JSON.generate(event: 'nevidimy_interactive_jobs',
                                    isolated: isolated, concurrency: config.total_concurrency,
                                    interactive_concurrency: isolated ? 1 : 0,
                                    background_concurrency: config.default_capsule.concurrency,
                                    pid: Process.pid))
  end
end

Rails.application.reloader.to_prepare do
  NevidimyInteractiveJobs.install
end

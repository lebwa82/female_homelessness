# Optional profile enrichment must not be part of accepting a Telegram message.
module NevidimyTelegramAvatar
  BOT_NAME = 'nevidima_bot_mvp_bot'.freeze
  COOLDOWN_SECONDS = 300
  LOOKUP_TIMEOUT_SECONDS = 5

  private

  def update_contact_avatar
    return super unless inbox.channel.bot_name == BOT_NAME

    enqueue_telegram_avatar
  end

  def enqueue_telegram_avatar
    return if @contact.avatar.attached?

    key = "nevidimy:telegram-avatar:#{@contact_inbox.id}"
    return unless Rails.cache.write(key, true, unless_exist: true, expires_in: COOLDOWN_SECONDS)

    NevidimyTelegramAvatarJob.perform_later(@contact_inbox.id)
  rescue StandardError => error
    # Redis/queue failures are optional-enrichment failures too. Never log the
    # exception message: HTTP/ActiveJob errors can contain bot tokens or URLs.
    Rails.logger.warn("Telegram avatar enqueue skipped kind=#{error.class.name}")
    nil
  end
end

Rails.application.reloader.to_prepare do
  Telegram::IncomingMessageService.prepend(NevidimyTelegramAvatar)
end

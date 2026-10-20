require 'timeout'

class NevidimyTelegramAvatarJob < Avatar::AvatarFromUrlJob
  queue_as :low

  def perform(contact_inbox_id)
    link = ContactInbox.find_by(id: contact_inbox_id)
    return unless link && link.inbox.channel_type == 'Channel::Telegram'
    return unless NevidimyTelegramAvatar::BOT_NAMES.include?(link.inbox.channel.bot_name)
    return if link.contact.avatar.attached?

    # Reuse Chatwoot's native metadata lookup. Its two read-only HTTP calls have
    # no explicit timeout; bound their combined duration outside the message job.
    url = Timeout.timeout(NevidimyTelegramAvatar::LOOKUP_TIMEOUT_SECONDS) do
      link.inbox.channel.get_telegram_profile_image(link.source_id)
    end
    # Keep the token-bearing file URL out of ActiveJob arguments and logs.
    # The native downloader runs inside this job, whose only argument is a DB ID.
    super(link.contact, url) if url
  rescue StandardError => error
    Rails.logger.warn("Telegram avatar lookup skipped kind=#{error.class.name}")
    nil
  end

  private

  def fetch_and_attach_avatar(avatarable, avatar_url)
    super
  rescue SafeFetch::Error => error
    # The native job logs the complete URL on download failures. For Telegram
    # that URL contains the bot token, so handle the optional failure here.
    Rails.logger.warn("Telegram avatar download skipped kind=#{error.class.name}")
    nil
  end
end

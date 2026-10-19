# Keep Telegram actions machine-readable without showing callback codes to staff.
# Deploy the compatible Agent Bot parser before enabling this initializer.
module NevidimyTelegramCallback
  BOT_NAMES = %w[nevidima_bot_mvp_bot nevidima_bot].freeze
  DATA_ATTRIBUTE = 'nevidimy_telegram_callback_data'.freeze

  private

  def nevidimy_callback?
    BOT_NAMES.include?(inbox.channel.bot_name) && callback_query_params? &&
      callback_query_data.is_a?(String) && callback_query_data.bytesize.between?(1, 64)
  end

  def telegram_params_message_content
    return super unless nevidimy_callback?

    keyboard = params.dig(:callback_query, :message, :reply_markup, :inline_keyboard)
    button = keyboard.flatten(1).find do |item|
      item.respond_to?(:key?) && item[:callback_data] == callback_query_data
    end if keyboard.is_a?(Array)
    label = button&.[](:text)
    return 'Нажата кнопка Telegram (название недоступно).' unless label.is_a?(String) && !label.strip.empty?

    "Нажата кнопка: «#{label}»."
  end

  def telegram_params_content_attributes
    attributes = super
    return attributes unless nevidimy_callback?

    attributes.merge(DATA_ATTRIBUTE => callback_query_data)
  end
end

Rails.application.reloader.to_prepare do
  Telegram::IncomingMessageService.prepend(NevidimyTelegramCallback)
end

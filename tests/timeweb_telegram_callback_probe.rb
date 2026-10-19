require 'json'
require 'ostruct'

module Rails
  def self.application
    OpenStruct.new(reloader: Object.new.tap do |value|
      def value.to_prepare(&block)
        block.call
      end
    end)
  end
end

module Telegram
  class IncomingMessageService
    attr_reader :inbox, :params

    def initialize(name, params)
      @inbox = OpenStruct.new(channel: OpenStruct.new(bot_name: name))
      @params = params
    end

    def output
      { content: telegram_params_message_content,
        content_attributes: telegram_params_content_attributes }
    end

    private

    def callback_query_params?
      params.key?(:callback_query)
    end

    def callback_query_data
      params.dig(:callback_query, :data)
    end

    def telegram_params_message_content
      callback_query_params? ? callback_query_data : params.dig(:message, :text)
    end

    def telegram_params_content_attributes
      { 'in_reply_to_external_id' => 19 }
    end
  end
end

load File.expand_path('../deploy/timeweb/telegram_callback.rb', __dir__)
fixtures = JSON.parse(STDIN.read, symbolize_names: true)
puts JSON.generate(fixtures.map { |f| Telegram::IncomingMessageService.new(f[:bot_name], f[:params]).output })

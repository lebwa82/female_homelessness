require 'json'
require 'ostruct'
require 'logger'
require 'stringio'

module Rails
  class Cache
    attr_accessor :fail
    attr_reader :writes

    def initialize
      @writes = {}
    end

    def write(key, value, unless_exist:, expires_in:)
      raise 'private-queue-credential' if fail
      raise 'missing deduplication or expiry' unless unless_exist && expires_in == 300
      return false if @writes.key?(key)

      @writes[key] = value
    end
  end

  def self.cache
    @cache ||= Cache.new
  end

  def self.logger
    @logger ||= Logger.new($probe_log)
  end

  def self.application
    OpenStruct.new(reloader: Object.new.tap do |value|
      def value.to_prepare(&block)
        block.call
      end
    end)
  end
end

class ApplicationJob
  def self.queue_as(name)
    @queue = name
  end

  def self.queue_name
    @queue
  end

  def self.perform_later(*args)
    raise 'private-queue-credential' if $enqueue_failure

    $jobs << [self, args]
  end
end

module Avatar
  class AvatarFromUrlJob < ApplicationJob
    def perform(contact, url)
      fetch_and_attach_avatar(contact, url)
    end

    private

    def fetch_and_attach_avatar(contact, url)
      raise $download_error if $download_error

      $downloads << [contact, url]
    end
  end
end

module SafeFetch
  class Error < StandardError; end
end

class ContactInbox
  def self.find_by(id:)
    $links[id]
  end
end

module Telegram
  class IncomingMessageService
    attr_reader :inbox, :stored

    def initialize(link)
      @inbox, @contact, @contact_inbox = link.inbox, link.contact, link
    end

    def perform
      update_contact_avatar
      @stored = true
    end

    private

    def update_contact_avatar
      raise 'native-avatar-error'
    end
  end
end

def verify(value)
  raise 'avatar probe assertion failed' unless value
end

def link(id, name = 'nevidima_bot_mvp_bot', attached = false)
  avatar = OpenStruct.new(attached?: attached)
  channel = OpenStruct.new(bot_name: name)
  def channel.get_telegram_profile_image(_user_id)
    $lookups += 1
    sleep($lookup_sleep) if $lookup_sleep
    raise $lookup_error if $lookup_error

    $avatar_url
  end
  OpenStruct.new(id: id, source_id: 'synthetic-contact', contact: OpenStruct.new(avatar: avatar),
                 inbox: OpenStruct.new(channel: channel, channel_type: 'Channel::Telegram'))
end

$probe_log = StringIO.new
$jobs, $links, $lookups, $downloads = [], {}, 0, []
require_relative '../deploy/timeweb/telegram_avatar'
require_relative '../deploy/timeweb/telegram_avatar_job'

$links[1] = link(1)
service = Telegram::IncomingMessageService.new($links[1])
service.perform
verify(service.stored && $lookups.zero?)
verify($jobs == [[NevidimyTelegramAvatarJob, [1]]])
service.perform
verify($jobs.size == 1 && $lookups.zero?)
verify(NevidimyTelegramAvatarJob.queue_name == :low)

$lookup_error = Timeout::Error.new('private-avatar-token')
NevidimyTelegramAvatarJob.new.perform(1)
verify(service.stored && $lookups == 1 && $jobs.size == 1)
verify(!$probe_log.string.include?('private-avatar-token'))

$lookup_error = nil
$lookup_sleep = 1
NevidimyTelegramAvatar.send(:remove_const, :LOOKUP_TIMEOUT_SECONDS)
NevidimyTelegramAvatar.const_set(:LOOKUP_TIMEOUT_SECONDS, 0.02)
started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
NevidimyTelegramAvatarJob.new.perform(1)
verify(Process.clock_gettime(Process::CLOCK_MONOTONIC) - started < 0.5)
verify($jobs.size == 1)
NevidimyTelegramAvatar.send(:remove_const, :LOOKUP_TIMEOUT_SECONDS)
NevidimyTelegramAvatar.const_set(:LOOKUP_TIMEOUT_SECONDS, 5)
$lookup_sleep = nil

$lookup_error = nil
$avatar_url = 'https://api.telegram.org/file/bot-test-only/avatar.jpg'
NevidimyTelegramAvatarJob.new.perform(1)
verify($downloads.last == [$links[1].contact, $avatar_url])
verify($jobs == [[NevidimyTelegramAvatarJob, [1]]])
$download_error = SafeFetch::Error.new('private-avatar-token')
NevidimyTelegramAvatarJob.new.perform(1)
verify(!$probe_log.string.include?('private-avatar-token'))
$download_error = nil

count = $jobs.size
NevidimyTelegramAvatarJob.new.perform(999)
$links[2] = link(2, 'unrelated_bot')
NevidimyTelegramAvatarJob.new.perform(2)
$links[3] = link(3, 'nevidima_bot_mvp_bot', true)
Telegram::IncomingMessageService.new($links[3]).perform
NevidimyTelegramAvatarJob.new.perform(3)
verify($jobs.size == count)

begin
  Telegram::IncomingMessageService.new($links[2]).perform
  raise 'unrelated bot behavior changed'
rescue RuntimeError => error
  verify(error.message == 'native-avatar-error')
end

$links[4] = link(4)
Rails.cache.fail = true
service = Telegram::IncomingMessageService.new($links[4])
service.perform
verify(service.stored && $jobs.size == count)
Rails.cache.fail = false
$enqueue_failure = true
service.perform
verify(service.stored && !$probe_log.string.include?('private-queue-credential'))

$enqueue_failure = false
$links[5] = link(5, 'nevidima_bot')
production = Telegram::IncomingMessageService.new($links[5])
lookups_before = $lookups
production.perform
verify(production.stored && $lookups == lookups_before)
verify($jobs.last == [NevidimyTelegramAvatarJob, [5]])
NevidimyTelegramAvatarJob.new.perform(5)
verify($lookups == lookups_before + 1)

puts JSON.generate(message_saved_without_lookup: true, avatar_deduplicated: true,
                   timeout_is_optional: true, queue_failure_is_optional: true,
                   total_deadline_enforced: true,
                   native_download_reused: true, production_avatar_is_optional: true,
                   unrelated_bots_unchanged: true,
                   secrets_not_logged: true)

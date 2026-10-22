# Exact pinned method bodies and synthetic collaborators; no Rails, data or network.
require 'json'
require 'ostruct'
require 'singleton'
require 'tempfile'

def verify(value, reason)
  raise reason unless value
end

class Object
  def blank?
    nil? || self == false || (respond_to?(:empty?) && empty?)
  end

  def present?
    !blank?
  end
end

module Events
  module Types
    CONVERSATION_READ = 'conversation.read'
    CONVERSATION_UPDATED = 'conversation.updated'
    TEAM_CHANGED = 'team.changed'
    ASSIGNEE_CHANGED = 'assignee.changed'
    CONVERSATION_STATUS_CHANGED = 'conversation.status.changed'
  end
end

class BaseListener
  include Singleton

  def extract_conversation_and_account(event)
    conversation = event.data.fetch(:conversation)
    [conversation, conversation.account]
  end
end

class ApplicationJob
  class << self
    attr_accessor :enqueued, :failure

    def queue_as(*); end

    def perform_later(*arguments)
      raise failure if failure

      self.enqueued ||= []
      enqueued << arguments
    end
  end
end

module Current
  class << self
    attr_accessor :user
  end
end

module ActionCable
  def self.server
    self
  end

  def self.broadcast(member, payload)
    (@broadcasts ||= []) << [member, payload]
  end

  def self.reset
    @broadcasts = []
  end

  def self.broadcasts
    @broadcasts
  end
end

LISTENER_SOURCE = <<~'RUBY'
class ActionCableListener < BaseListener
  include Events::Types

  def conversation_updated(event)
    conversation, account = extract_conversation_and_account(event)
    tokens = user_tokens(account, conversation.inbox.members) + contact_inbox_tokens(conversation.contact_inbox)

    broadcast(account, tokens, CONVERSATION_UPDATED, conversation.push_event_data)
  end

  def assignee_changed(event)
    conversation, account = extract_conversation_and_account(event)
    tokens = user_tokens(account, conversation.inbox.members)

    broadcast(account, tokens, ASSIGNEE_CHANGED, conversation.push_event_data)
  end

  def message_created(event)
    broadcast(event.data[:conversation].account, ['message-member'], 'message.created', {message: true})
  end

  private

  def user_tokens(account, agents)
    agent_tokens = agents.pluck(:pubsub_token)
    admin_tokens = account.administrators.pluck(:pubsub_token)
    (agent_tokens + admin_tokens).uniq
  end

  def contact_inbox_tokens(contact_inbox)
    contact = contact_inbox.contact

    contact_inbox.hmac_verified? ? contact.contact_inboxes.where(hmac_verified: true).filter_map(&:pubsub_token) : [contact_inbox.pubsub_token]
  end

  def broadcast(account, tokens, event_name, data)
    return if tokens.blank?

    payload = data.merge(account_id: account.id)
    # So the frondend knows who performed the action.
    # Useful in cases like conversation assignment for generating a notification with assigner name.
    payload[:performer] = Current.user&.push_event_data if Current.user.present?

    ::ActionCableBroadcastJob.perform_later(tokens.uniq, event_name, payload)
  end
end
RUBY

JOB_SOURCE = <<~'RUBY'
class ActionCableBroadcastJob < ApplicationJob
  queue_as :critical
  include Events::Types

  CONVERSATION_UPDATE_EVENTS = [
    CONVERSATION_READ,
    CONVERSATION_UPDATED,
    TEAM_CHANGED,
    ASSIGNEE_CHANGED,
    CONVERSATION_STATUS_CHANGED
  ].freeze

  def perform(members, event_name, data)
    return if members.blank?

    broadcast_data = prepare_broadcast_data(event_name, data)
    broadcast_to_members(members, event_name, broadcast_data)
  end

  private

  # Ensures that only the latest available data is sent to prevent UI issues
  # caused by out-of-order events during high-traffic periods. This prevents
  # the conversation job from processing outdated data.
  def prepare_broadcast_data(event_name, data)
    return data unless CONVERSATION_UPDATE_EVENTS.include?(event_name)

    account = Account.find(data[:account_id])
    conversation = account.conversations.find_by!(display_id: data[:id])
    broadcast_data = conversation.push_event_data.merge(account_id: data[:account_id])
    if event_name == ASSIGNEE_CHANGED
      # Keep the original assignment together even when the conversation is
      # refreshed. The dashboard checks its identity against the current owner.
      # Other events omit this field so unrelated updates cannot overwrite it.
      broadcast_data[:assignment] = {
        automatic: data[:automatic_assignment] == true,
        assignee_id: data.dig(:meta, :assignee, :id),
        assignee_type: data.dig(:meta, :assignee_type),
        updated_at: data[:updated_at]
      }
    end
    broadcast_data
  end

  def broadcast_to_members(members, event_name, broadcast_data)
    members.each do |member|
      ActionCable.server.broadcast(
        member,
        {
          event: event_name,
          data: broadcast_data
        }
      )
    end
  end
end
RUBY

class ProbeTokens
  attr_accessor :failure

  def initialize(tokens)
    @tokens = tokens
  end

  def pluck(attribute)
    raise failure if failure
    verify(attribute == :pubsub_token, 'token projection changed')
    @tokens
  end
end

class ProbeConversation
  attr_accessor :version, :serializations
  attr_reader :account, :inbox, :contact_inbox, :display_id

  def initialize
    @display_id, @version, @serializations = 7, 1, 0
    @account = OpenStruct.new(id: 3, administrators: ProbeTokens.new(%w[admin shared]))
    @inbox = OpenStruct.new(members: ProbeTokens.new(%w[agent shared]))
    @contact_inbox = OpenStruct.new(contact: Object.new, pubsub_token: 'contact', hmac_verified?: false)
  end

  def id
    99 # Intentionally different from account-local display_id.
  end

  def push_event_data
    @serializations += 1
    {id: display_id, version: version, meta: {assignee: {id: version}}, updated_at: version.to_f,
     custom_attributes: {technical: true}}
  end
end

class Account
  class << self
    attr_accessor :conversation, :lookups, :failure

    def find(id)
      raise failure if failure
      verify(id == conversation.account.id, 'wrong account lookup')
      (self.lookups ||= []) << [:account, id]
      self
    end

    def conversations
      self
    end

    def find_by!(display_id:)
      verify(display_id == conversation.display_id, 'database id used instead of display_id')
      lookups << [:display, display_id]
      conversation
    end
  end
end

module Rails
  class Reloader
    def initialize
      @callbacks = []
    end

    def to_prepare(&block)
      @callbacks << block
    end

    def prepare
      @callbacks.each(&:call)
    end
  end

  def self.application
    @application ||= OpenStruct.new(reloader: Reloader.new)
  end
end

def write_source(file, source)
  file.rewind
  file.write(source)
  file.truncate(file.pos)
  file.flush
end

def reload_native(listener, job)
  %i[ActionCableListener ActionCableBroadcastJob].each do |name|
    Object.send(:remove_const, name) if Object.const_defined?(name, false)
  end
  load listener.path
  load job.path
  ActionCableBroadcastJob.enqueued = []
  ActionCable.reset
end

def run_worker(arguments)
  ActionCable.reset
  Account.lookups = []
  ActionCableBroadcastJob.new.perform(*arguments)
  ActionCable.broadcasts
end

report = {}
Tempfile.create(['native-conversation-listener', '.rb']) do |listener|
  Tempfile.create(['native-conversation-broadcast-job', '.rb']) do |job|
    write_source(listener, LISTENER_SOURCE)
    write_source(job, JOB_SOURCE)
    reload_native(listener, job)
    verify(ActionCableListener.private_methods.include?(:new) &&
           ActionCableListener.instance.equal?(ActionCableListener.instance), 'native Singleton contract changed')
    report[:native_singleton_listener_contract] = true
    conversation = ProbeConversation.new
    event = OpenStruct.new(data: {conversation: conversation})
    Current.user = OpenStruct.new(push_event_data: {id: 5, type: 'synthetic'})
    Account.conversation = conversation
    ActionCableListener.instance.conversation_updated(event)
    native_arguments = ActionCableBroadcastJob.enqueued.last
    conversation.version = 2
    expected = run_worker(native_arguments)

    initializer = File.expand_path('../deploy/timeweb/conversation_broadcast_fast_path.rb', __dir__)
    load initializer if File.file?(initializer)
    Rails.application.reloader.prepare
    conversation.version, conversation.serializations = 1, 0
    ActionCableBroadcastJob.enqueued = []
    ActionCableListener.instance.conversation_updated(event)
    optimized_arguments = ActionCableBroadcastJob.enqueued.last
    verify(conversation.serializations.zero?, 'discarded conversation serializer still ran')
    verify(optimized_arguments[2] == {id: 7, account_id: 3, performer: {id: 5, type: 'synthetic'}}, 'projection identity changed')
    verify(optimized_arguments[0..1] == native_arguments[0..1], 'recipient tokens or event changed')
    report[:skip_only_discarded_serializer_with_identical_recipients] = true

    conversation.version = 2
    verify(run_worker(optimized_arguments) == expected, 'fresh native worker output differs')
    verify(Account.lookups == [[:account, 3], [:display, 7]], 'account/display scope changed')
    verify(conversation.serializations == 1, 'worker no longer serializes fresh data')
    report[:identical_native_output_after_worker_refresh] = true

    ActionCableBroadcastJob.enqueued = []
    ActionCableListener.instance.assignee_changed(event)
    verify(ActionCableBroadcastJob.enqueued.last[2].key?(:meta), 'assignment snapshot was projected')
    assignment = run_worker(ActionCableBroadcastJob.enqueued.last).first[1][:data][:assignment]
    verify(assignment[:assignee_id] == 2 && assignment[:updated_at] == 2.0, 'native assignment details changed')
    ActionCableListener.instance.message_created(event)
    verify(ActionCableBroadcastJob.enqueued.last[2][:message], 'message event changed')
    report[:all_other_events_remain_native] = true

    failure = RuntimeError.new('synthetic permission lookup failure')
    conversation.inbox.members.failure = failure
    begin
      ActionCableListener.instance.conversation_updated(event)
      raise 'permission lookup exception swallowed'
    rescue RuntimeError => error
      verify(error.equal?(failure), 'permission lookup exception replaced')
    ensure
      conversation.inbox.members.failure = nil
    end
    ActionCableBroadcastJob.failure = failure
    begin
      ActionCableListener.instance.conversation_updated(event)
      raise 'enqueue exception swallowed'
    rescue RuntimeError => error
      verify(error.equal?(failure), 'enqueue exception replaced')
    ensure
      ActionCableBroadcastJob.failure = nil
    end
    report[:native_lookup_and_enqueue_exceptions_propagate] = true

    Rails.application.reloader.prepare
    verify(ActionCableListener.ancestors.count(NevidimyConversationBroadcastFastPath::Listener) == 1, 'reload duplicated wrapper')
    report[:reload_is_idempotent] = true

    [[listener, LISTENER_SOURCE, 'CONVERSATION_UPDATED, conversation.push_event_data'],
     [listener, LISTENER_SOURCE, 'payload = data.merge(account_id: account.id)'],
     [job, JOB_SOURCE, 'broadcast_data = prepare_broadcast_data(event_name, data)'],
     [job, JOB_SOURCE, 'account = Account.find(data[:account_id])']].each do |file, source, text|
      write_source(file, source.sub(text, "#{text} "))
      reload_native(listener, job)
      Rails.application.reloader.prepare
      verify(!(ActionCableListener < NevidimyConversationBroadcastFastPath::Listener), 'unknown source enabled projection')
      conversation.serializations = 0
      ActionCableListener.instance.conversation_updated(event)
      verify(conversation.serializations == 1, 'unknown source did not retain native serialization')
      write_source(file, source)
    end
    report[:changed_source_keeps_native_fallback] = true

    reload_native(listener, job)
    ActionCableBroadcastJob.prepend(Module.new do
      def perform(*arguments)
        super
      end
    end)
    Rails.application.reloader.prepare
    verify(!(ActionCableListener < NevidimyConversationBroadcastFastPath::Listener), 'unknown worker override enabled projection')
    verify(!NevidimyConversationBroadcastFastPath.native_contract?(Class.new, Class.new), 'missing contract accepted')
    report[:unknown_owner_or_missing_contract_keeps_native] = true

    reload_native(listener, job)
    ActionCableBroadcastJob.send(:remove_const, :CONVERSATION_UPDATE_EVENTS)
    ActionCableBroadcastJob.const_set(:CONVERSATION_UPDATE_EVENTS, [].freeze)
    Rails.application.reloader.prepare
    verify(!(ActionCableListener < NevidimyConversationBroadcastFastPath::Listener), 'missing worker refresh enabled projection')
    report[:worker_refresh_membership_is_required] = true

    reload_native(listener, job)
    Rails.application.reloader.prepare
    timing = Module.new do
      def conversation_updated(event)
        (@timed_events ||= []) << :conversation_updated
        super
      end

      attr_reader :timed_events
    end
    ActionCableListener.prepend(timing)
    timed = ActionCableListener.instance
    conversation.serializations = 0
    timed.conversation_updated(event)
    verify(timed.timed_events == [:conversation_updated] && conversation.serializations.zero?, 'outer timing wrapper bypassed')
    report[:later_timing_wrapper_is_preserved] = true

    reload_native(listener, job)
    ActionCableListener.prepend(timing)
    Rails.application.reloader.prepare
    verify(!(ActionCableListener < NevidimyConversationBroadcastFastPath::Listener), 'earlier unknown listener override accepted')
    report[:earlier_unknown_listener_override_keeps_native] = true
  end
end
puts JSON.generate(report)

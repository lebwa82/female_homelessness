# Exact pinned native source with neutral in-memory collaborators only.
require 'delegate'
require 'json'
require 'ostruct'
require 'tempfile'

def verify(value, reason)
  raise reason unless value
end

class Object
  def try(name)
    public_send(name) if respond_to?(name)
  end
end

module Conversations; end
module Enterprise
  module Conversations; end
end

NATIVE_SOURCE = <<~'RUBY'
class Conversations::EventDataPresenter < SimpleDelegator
  def push_data
    {
      additional_attributes: additional_attributes,
      can_reply: can_reply?,
      channel: inbox.try(:channel_type),
      contact_inbox: contact_inbox,
      id: display_id,
      inbox_id: inbox_id,
      messages: push_messages,
      labels: label_list,
      meta: push_meta,
      status: status,
      custom_attributes: custom_attributes,
      snoozed_until: snoozed_until,
      unread_count: unread_incoming_messages.count,
      first_reply_created_at: first_reply_created_at,
      priority: priority,
      waiting_since: waiting_since.to_i,
      **push_timestamps
    }
  end

  # Like #push_data but with message text normalized for external integrations (webhooks).
  def webhook_data
    push_data.merge(
      account: account.webhook_data,
      messages: webhook_push_messages
    )
  end

  private

  def push_messages
    [messages.where(account_id: account_id).chat.last&.push_event_data].compact
  end

  def webhook_push_messages
    [messages.where(account_id: account_id).chat.last&.webhook_push_event_data].compact
  end

  def push_meta
    {
      sender: contact.push_event_data,
      assignee: assigned_entity&.push_event_data,
      assignee_type: assignee_type,
      team: team&.push_event_data,
      hmac_verified: contact_inbox&.hmac_verified
    }
  end

  def push_timestamps
    {
      agent_last_seen_at: agent_last_seen_at.to_i,
      contact_last_seen_at: contact_last_seen_at.to_i,
      last_activity_at: last_activity_at.to_i,
      timestamp: last_activity_at.to_i,
      created_at: created_at.to_i,
      updated_at: updated_at.to_f
    }
  end
end
Conversations::EventDataPresenter.prepend_mod_with('Conversations::EventDataPresenter')
RUBY

EE_SOURCE = <<~'RUBY'
module Enterprise::Conversations::EventDataPresenter
  def push_data
    return super unless account.feature_enabled?('sla')

    sla_applicable = sla_applicable?

    super.merge(
      applied_sla: sla_applicable ? applied_sla&.push_event_data : nil,
      sla_events: sla_applicable ? sla_events.map(&:push_event_data) : [],
      sla_policy_id: sla_applicable ? sla_policy_id : nil
    )
  end
end
RUBY

class ProbeAccount
  attr_accessor :enabled, :failure, :hook

  def initialize(enabled)
    @enabled = enabled
  end

  def feature_enabled?(name)
    verify(name == 'sla', 'unexpected feature read')
    enabled
  end

  def webhook_data
    raise failure if failure

    hook ? { id: 9, nested: hook.call } : { id: 9 }
  end
end

class ProbeMessage
  attr_reader :push_calls, :webhook_calls
  attr_accessor :id, :failure

  def initialize
    @id, @push_calls, @webhook_calls = 11, 0, 0
  end

  def push_event_data
    @push_calls += 1
    { id: id, content: 'synthetic original', metadata: { kind: 'synthetic' } }
  end

  def webhook_push_event_data
    @webhook_calls += 1
    raise failure if failure

    push_event_data.merge(content: 'synthetic normalized')
  end
end

class ProbeMessages
  attr_reader :last_calls, :message

  def initialize(message)
    @message, @last_calls = message, 0
  end

  def where(account_id:)
    verify(account_id == 9, 'account scope changed')
    self
  end

  def chat
    self
  end

  def last
    @last_calls += 1
    message
  end
end

class ProbeConversation < OpenStruct
  attr_accessor :additional_hook

  def additional_attributes
    hook = additional_hook
    if hook
      self.additional_hook = nil
      hook.call
    end
    { 'kind' => 'synthetic' }
  end
end

Metadata = Struct.new(:id) do
  def push_event_data
    { id: id }
  end
end
ContactInbox = Struct.new(:source_id, :hmac_verified)

def conversation(sla = false, applicable = true, empty = false)
  message = empty ? nil : ProbeMessage.new
  ProbeConversation.new(
    account: ProbeAccount.new(sla), account_id: 9, inbox: OpenStruct.new(channel_type: 'Synthetic'),
    contact_inbox: ContactInbox.new('synthetic', true), display_id: 101, inbox_id: 55,
    messages: ProbeMessages.new(message), label_list: ['synthetic'], contact: Metadata.new(3),
    assigned_entity: Metadata.new(4), assignee_type: 'User', team: Metadata.new(5), status: 'open',
    custom_attributes: { 'state' => 'synthetic' }, snoozed_until: nil, unread_incoming_messages: [1, 2],
    first_reply_created_at: nil, priority: nil, waiting_since: 10, agent_last_seen_at: 11,
    contact_last_seen_at: 12, last_activity_at: 13, created_at: 14, updated_at: 15,
    'can_reply?' => true, 'sla_applicable?' => applicable, applied_sla: Metadata.new(6),
    sla_events: [Metadata.new(7)], sla_policy_id: 8
  )
end

module Rails
  class Reloader
    def to_prepare(&block)
      @prepares ||= []
      @prepares << block
    end

    def prepare
      @prepares.each(&:call)
    end
  end

  def self.application
    @application ||= OpenStruct.new(reloader: Reloader.new)
  end
end

report = {}
Tempfile.create(['native-event-data-presenter', '.rb']) do |native|
  Tempfile.create(['enterprise-event-data-presenter', '.rb']) do |enterprise|
    native.write(NATIVE_SOURCE)
    native.flush
    enterprise.write(EE_SOURCE)
    enterprise.flush
    load enterprise.path
    presenter_class = Class.new(SimpleDelegator)
    Conversations.const_set(:EventDataPresenter, presenter_class)
    presenter_class.define_singleton_method(:prepend_mod_with) do |_name|
      prepend(Enterprise::Conversations::EventDataPresenter)
    end
    load native.path
    initializer = File.expand_path('../deploy/timeweb/webhook_presenter_fast_path.rb', __dir__)
    load initializer
    verify(NevidimyWebhookPresenterFastPath.native_contract?(presenter_class), 'pinned native/EE contract not recognized')
    verify(!NevidimyWebhookPresenterFastPath.native_contract?(Class.new), 'missing methods were enabled')
    verify(!NevidimyWebhookPresenterFastPath.native_contract?(Class.new(presenter_class)), 'unverified owner chain was enabled')
    enterprise.write("# unverified change\n")
    enterprise.flush
    verify(!NevidimyWebhookPresenterFastPath.native_contract?(presenter_class), 'unverified EE source was enabled')
    Rails.application.reloader.prepare
    verify(!(presenter_class < NevidimyWebhookPresenterFastPath::Presenter), 'unverified contract installed the patch')
    enterprise.truncate(0)
    enterprise.rewind
    enterprise.write(EE_SOURCE)
    enterprise.flush
    native.write("# unverified change\n")
    native.flush
    verify(!NevidimyWebhookPresenterFastPath.native_contract?(presenter_class), 'unverified base source was enabled')
    native.truncate(0)
    native.rewind
    native.write(NATIVE_SOURCE)
    native.flush
    report[:conservative_source_and_owner_gate] = true

    variants = [[false, true, false], [true, true, false], [true, false, false], [false, true, true]]
    expected = variants.map { |args| presenter_class.new(conversation(*args)).webhook_data }
    normal = presenter_class.new(conversation(true)).push_data
    Rails.application.reloader.prepare
    variants.each_with_index do |args, index|
      record = conversation(*args)
      actual = presenter_class.new(record).webhook_data
      verify(actual == expected[index], 'native webhook payload changed')
      verify(record.messages.last_calls == 1, 'discarded message lookup remained')
      verify(record.messages.message.nil? || record.messages.message.push_calls == 1, 'discarded message tree remained')
    end
    report[:exact_webhook_payload_with_all_sla_variants] = true
    verify(presenter_class.new(conversation(true)).push_data == normal, 'normal push_data changed')
    report[:normal_push_data_unchanged] = true

    record = conversation
    presenter = presenter_class.new(record)
    presenter.freeze
    presenter.webhook_data
    record.messages.message.id = 12
    verify(presenter.webhook_data[:messages].first[:id] == 12, 'message data cached across calls')
    verify(record.messages.last_calls == 2, 'frozen presenter skipped final native lookup')
    report[:frozen_presenter_without_payload_caching] = true

    [[:account, :failure], [:message, :failure]].each do |target, field|
      record = conversation
      presenter = presenter_class.new(record)
      failure = IOError.new('synthetic native failure')
      (target == :account ? record.account : record.messages.message).public_send("#{field}=", failure)
      previous = { presenter: Object.new, pending: true }
      Thread.current[NevidimyWebhookPresenterFastPath::SCOPE_KEY] = previous
      begin
        presenter.webhook_data
        raise 'native error was swallowed'
      rescue IOError => caught
        verify(caught.equal?(failure), 'native error identity changed')
      end
      verify(Thread.current[NevidimyWebhookPresenterFastPath::SCOPE_KEY].equal?(previous), 'error scope not restored')
      Thread.current[NevidimyWebhookPresenterFastPath::SCOPE_KEY] = nil
      verify(presenter.push_data[:messages].first[:content] == 'synthetic original', 'error leaked skip scope')
    end
    report[:native_exceptions_restore_previous_scope] = true

    record = conversation
    presenter = presenter_class.new(record)
    record.account.hook = lambda do
      record.account.hook = nil
      nested = presenter.webhook_data
      { nested: nested, normal: presenter.push_data }
    end
    result = presenter.webhook_data
    verify(result[:account][:nested][:nested][:messages].first[:content] == 'synthetic normalized', 'nested webhook changed')
    verify(result[:account][:nested][:normal][:messages].first[:content] == 'synthetic original', 'one-shot scope changed later push_data')
    verify(record.messages.last_calls == 3, 'nested scope skipped or repeated native work')
    verify(Thread.current[NevidimyWebhookPresenterFastPath::SCOPE_KEY].nil?, 'nested scope leaked')
    report[:nested_calls_restore_one_shot_scope] = true

    other_record = conversation
    other = presenter_class.new(other_record)
    record = conversation
    presenter = presenter_class.new(record)
    record.additional_hook = -> { verify(other.push_data[:messages].first[:content] == 'synthetic original', 'other presenter inherited skip') }
    presenter.webhook_data
    verify(other_record.messages.last_calls == 1 && record.messages.last_calls == 1, 'presenter scopes crossed')
    report[:distinct_presenter_instances_are_isolated] = true

    record = conversation
    presenter = presenter_class.new(record)
    record.additional_hook = -> { Fiber.yield }
    fiber = Fiber.new do
      result = presenter.webhook_data
      [result, Thread.current[NevidimyWebhookPresenterFastPath::SCOPE_KEY]]
    end
    fiber.resume
    verify(presenter.push_data[:messages].first[:content] == 'synthetic original', 'fiber scope leaked')
    result, scope = fiber.resume
    verify(scope.nil? && result[:messages].first[:content] == 'synthetic normalized' &&
           record.messages.last_calls == 2, 'fiber restoration or native payload changed')
    report[:fiber_local_scope_isolation] = true

    load initializer
    2.times { Rails.application.reloader.prepare }
    verify(presenter_class.ancestors.count(NevidimyWebhookPresenterFastPath::Presenter) == 1, 'reload duplicated patch')
    verify(presenter_class.private_instance_methods.include?(:push_messages), 'native visibility changed')
    report[:reload_idempotency] = true
  end
end
puts JSON.generate(report)

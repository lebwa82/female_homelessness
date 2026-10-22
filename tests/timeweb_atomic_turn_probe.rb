# Exercises the actual initializer with synthetic controller/ORM collaborators.
# with_lock models rollback only: real DB locking, Rails transactions, native
# authentication callbacks, delivery, and after_commit are not integration-tested.
require 'json'
require 'ostruct'
require 'time'

class Object
  def present?
    self != false && !nil? && !(respond_to?(:empty?) && empty?)
  end
end

class Time
  def self.current
    now
  end
end

def verify(value, reason)
  raise reason unless value
end

module ActionController
  class Parameters
    def initialize(values = {})
      @values = values.each_with_object({}) do |(key, value), result|
        result[key.to_s] = wrap(value)
      end
    end

    def [](key)
      @values[key.to_s]
    end

    def []=(key, value)
      @values[key.to_s] = wrap(value)
    end

    def keys
      @values.keys
    end

    def key?(key)
      @values.key?(key.to_s)
    end

    def delete(key)
      @values.delete(key.to_s)
    end

    def permit(*fields)
      allowed = fields.flat_map { |field| field.is_a?(Hash) ? field.keys : field }.map(&:to_s)
      Parameters.new(to_unsafe_h.select { |key, _value| allowed.include?(key) })
    end

    def to_unsafe_h
      @values.transform_values { |value| unwrap(value) }
    end

    private

    def wrap(value)
      return Parameters.new(value) if value.is_a?(Hash)
      return value.map { |item| wrap(item) } if value.is_a?(Array)

      value
    end

    def unwrap(value)
      return value.to_unsafe_h if value.is_a?(Parameters)
      return value.map { |item| unwrap(item) } if value.is_a?(Array)

      value
    end
  end
end

class AgentBot
  attr_reader :id

  def initialize(id)
    @id = id
  end
end

module Current
  class << self
    attr_accessor :user
  end
end

class AgentBotInbox
  def self.exists?(inbox_id:, agent_bot_id:)
    inbox_id == 55 && agent_bot_id == 7
  end
end

class Relation
  include Enumerable

  def initialize(messages)
    @messages = messages
  end

  def where(filter, argument = nil)
    selected = if filter.is_a?(Hash)
                 @messages.select do |message|
                   filter.all? { |key, value| message.public_send(key).to_s == value.to_s }
                 end
               elsif filter == 'id > ?'
                 @messages.select { |message| message.id > argument }
               else
                 raise 'unexpected ORM query'
               end
    Relation.new(selected)
  end

  def find_by(filter)
    where(filter).first
  end

  def first
    @messages.first
  end

  def select(*fields)
    verify(fields == [:id, :content_attributes], 'reply lookup fetched unnecessary columns')
    self
  end

  def each(&block)
    @messages.each(&block)
  end

  def exists?
    !@messages.empty?
  end

  def pick(key)
    first&.public_send(key)
  end
end

def message(id, message_type = :incoming, private_message = false, sender_type = 'Contact', key = nil)
  OpenStruct.new(id: id, message_type: message_type, private: private_message,
                 sender_type: sender_type, content_attributes: { 'bot_turn_key' => key })
end

class Conversation
  attr_accessor :status, :assignee_id, :team_id, :custom_attributes, :updated_at, :rows
  attr_reader :lock_count, :writes, :locked

  class << self
    attr_accessor :staff_locks

    def where(id:)
      staff_locks << [:where, id]
      self
    end

    def lock(value)
      staff_locks << [:lock, value]
      self
    end

    def pick(value)
      staff_locks << [:pick, value]
      900
    end
  end
  self.staff_locks = []

  def initialize
    @status = 'open'
    @assignee_id = @team_id = nil
    @custom_attributes = { 'reply_owner' => 'bot', 'ownership_version' => 3,
                           'context_epoch' => 2, 'unrelated' => 'preserve' }
    @updated_at = Time.utc(2026, 1, 1, 0, 0, 0, 123456)
    @rows = [message(10)]
    @lock_count, @writes = 0, 0
  end

  def inbox_id
    55
  end

  def display_id
    101
  end

  def messages
    Relation.new(rows)
  end

  def with_lock
    snapshot = Marshal.dump([custom_attributes, updated_at, rows])
    @lock_count += 1
    @locked = true
    yield
  rescue StandardError
    self.custom_attributes, self.updated_at, self.rows = Marshal.load(snapshot)
    raise
  ensure
    @locked = false
  end

  def update_columns(custom_attributes:, updated_at:)
    verify(locked, 'workflow update escaped conversation lock')
    @writes += 1
    self.custom_attributes = custom_attributes
    self.updated_at = updated_at
  end
end

module Messages
  class MessageBuilder
    class << self
      attr_accessor :fail, :fail_at, :calls
    end
    self.calls = []

    def initialize(user, conversation, payload)
      @user, @conversation, @payload = user, conversation, payload
    end

    def perform
      verify(@conversation.locked, 'native builder escaped conversation lock')
      self.class.calls << [@user, @conversation, @payload.to_unsafe_h]
      raise IOError, 'synthetic builder failure' if self.class.fail || self.class.fail_at == self.class.calls.size

      result = message(100 + @conversation.rows.size, :outgoing, @payload[:private], 'AgentBot')
      result.content_attributes = @payload[:content_attributes].to_unsafe_h
      result.content = @payload[:content]
      @conversation.rows << result
      result
    end
  end
end

class BaseController
  attr_reader :response, :native_calls

  def initialize(conversation, params = {})
    @conversation = conversation
    @params = ActionController::Parameters.new(params)
    @native_calls = 0
  end

  def params
    @params
  end

  def head(status)
    @response = { status: status }
  end

  def render(json:, status: :ok)
    @response = { status: status, json: json }
  end

  def show
    @native_calls += 1
    @response = :native
  end

  alias create show
end

module Api
  module V1
    module Accounts
      class ConversationsController < BaseController; end

      module Conversations
        class MessagesController < BaseController; end
      end
    end
  end
end

class Message
  class << self
    def callbacks
      @callbacks ||= []
    end

    def before_create(callback)
      callbacks << callback
    end
  end

  def initialize(outgoing, private_message, sender)
    @outgoing, @private_message, @sender = outgoing, private_message, sender
  end

  def outgoing?
    @outgoing
  end

  def private?
    @private_message
  end

  def sender_type
    @sender
  end

  def conversation_id
    900
  end

  def run_callbacks
    self.class.callbacks.each { |callback| send(callback) }
  end
end

module Rails
  class Reloader
    def to_prepare(&block)
      @prepare = block
      prepare
    end

    def prepare
      @prepare.call
    end
  end

  def self.application
    @application ||= OpenStruct.new(reloader: Reloader.new)
  end
end

require_relative '../deploy/timeweb/atomic_turn'

Projection = Api::V1::Accounts::ConversationsController
Commit = Api::V1::Accounts::Conversations::MessagesController

def payload(conversation, turn_changes = {}, outer_changes = {})
  turn = { input_id: '10', version: NevidimyAtomicTurn.version(conversation),
           attributes: { 'workflow_state' => 'synthetic', 'scenario_last_message_id' => 10 } }
  { nevidimy_atomic_turn: turn.merge(turn_changes), content: 'synthetic' }.merge(outer_changes)
end

def commit(conversation, values = payload(conversation))
  controller = Commit.new(conversation, values)
  controller.create
  controller
end

def unchanged?(conversation)
  conversation.writes.zero? && conversation.rows.size == 1 &&
    conversation.custom_attributes['workflow_state'].nil?
end

report = {}
Current.user = AgentBot.new(7)
3.times { Rails.application.reloader.prepare }
verify(Projection.ancestors.count(NevidimyAtomicTurn::Projection) == 1, 'projection registered repeatedly')
verify(Commit.ancestors.count(NevidimyAtomicTurn::Commit) == 1, 'commit registered repeatedly')
verify(Message.callbacks == [:nevidimy_lock_staff_conversation], 'staff callback registration is not idempotent')
report['reload_registration_is_idempotent'] = true

conversation = Conversation.new
verify(Projection.new(conversation).show == :native, 'native projection route changed')
verify(Commit.new(conversation).create == :native, 'native message route changed')
projection = Projection.new(conversation, nevidimy_turn_state: '10')
projection.show
state = projection.response.fetch(:json)
verify(state[:capability] == 'nevidimy-atomic-turn-v1' && state[:input_id] == 10, 'wrong projection identity')
verify(state[:features] == ['private_notes_v1'], 'private note feature was not advertised')
verify(state[:eligible] && state[:reply_id].nil?, 'wrong projection eligibility')
verify(state[:version] == '2026-01-01T00:00:00.123456Z', 'projection version lost precision')
verify(state[:conversation][:id] == 101 && state[:conversation][:meta][:assignee_type] == 'AgentBot', 'wrong native conversation projection')
%w[0 010 unknown 11].each do |id|
  missing = Projection.new(conversation, nevidimy_turn_state: id)
  missing.show
  verify(missing.response[:status] == :not_found, 'invalid or missing input was projected')
end
report['projection_and_native_pass_through'] = true

[nil, Object.new, AgentBot.new(8)].each do |user|
  Current.user = user
  denied = Conversation.new
  result = commit(denied)
  verify(result.response[:status] == :forbidden && denied.lock_count.zero? && unchanged?(denied), 'unbound identity mutated conversation')
end
Current.user = AgentBot.new(7)
report['bound_agent_bot_authorization'] = true

invalid_turns = [
  { input_id: '0' }, { input_id: '010' }, { input_id: '1' * 20 },
  { version: nil }, { version: 'x' * 41 }, { unexpected: true },
  { attributes: 'invalid' }, { attributes: {} },
  { attributes: { 'reply_owner' => 'staff', 'scenario_last_message_id' => 10 } },
  { attributes: { 'context_epoch' => 3, 'scenario_last_message_id' => 10 } },
  { attributes: { 'scenario_pending_input' => 'synthetic', 'scenario_last_message_id' => 10 } },
  { attributes: { 'scenario_last_message_id' => '10' } },
  { attributes: { 'scenario_last_message_id' => 10, 'scenario' => 'x' * 262_145 } }
]
invalid_turns.each do |changes|
  invalid = Conversation.new
  verify(commit(invalid, payload(invalid, changes)).response[:status] == :conflict && unchanged?(invalid), 'invalid turn mutated conversation')
end
[{ content: nil }, { content: 'x' * 32_769 }, { content_type: 'unsupported' },
 { attachments: ['synthetic'] }, { nevidimy_atomic_turn: nil }].each do |changes|
  invalid = Conversation.new
  verify(commit(invalid, payload(invalid, {}, changes)).response[:status] == :conflict && unchanged?(invalid), 'invalid message payload mutated conversation')
end
report['strict_validation_and_workflow_allowlist'] = true

mutations = [
  ->(value) { value.status = 'resolved' },
  ->(value) { value.assignee_id = 9 },
  ->(value) { value.custom_attributes['reply_owner'] = 'staff' },
  ->(value) { value.custom_attributes['ownership_version'] = '3' },
  ->(value) { value.custom_attributes['scenario_pending_input'] = 'synthetic' },
  ->(value) { value.rows << message(11, :outgoing, false, 'User') }
]
%w[scenario_last_message_id last_clear_message_id ownership_last_staff_message_id].each do |key|
  mutations << ->(value) { value.custom_attributes[key] = 11 }
end
mutations.each do |change|
  conflict = Conversation.new
  change.call(conflict)
  rows_before = conflict.rows.size
  verify(!NevidimyAtomicTurn.eligible?(conflict, 10), 'ineligible conversation accepted')
  verify(commit(conflict).response[:status] == :conflict && conflict.writes.zero? && conflict.rows.size == rows_before, 'eligibility conflict partially mutated conversation')
end
conflict = Conversation.new
stale = commit(conflict, payload(conflict, version: 'stale')).response
verify(stale[:status] == :conflict && unchanged?(conflict), 'stale version mutated conversation')
verify(stale[:json] == { outcome: 'conflict', reason: 'stale_version' }, 'stale no-write reason missing')
ineligible = Conversation.new
ineligible.assignee_id = 9
verify(!commit(ineligible).response.key?(:json), 'generic conflict advertised stale retry')
missing = Conversation.new
missing.rows.clear
verify(commit(missing).response[:status] == :conflict && missing.writes.zero?, 'missing input mutated conversation')
report['eligibility_and_version_conflicts_have_no_writes'] = true

eligible = Conversation.new
eligible.status = 'pending'
eligible.rows.concat([message(11, :outgoing, true, 'User'), message(12, :outgoing, false, 'AgentBot'), message(9, :outgoing, false, 'User')])
%w[scenario_last_message_id last_clear_message_id ownership_last_staff_message_id].each { |key| eligible.custom_attributes[key] = 10 }
verify(NevidimyAtomicTurn.eligible?(eligible, 10), 'non-conflicting history blocked bot')
report['private_old_and_bot_messages_do_not_block'] = true

successful = Conversation.new
values = payload(successful, {}, message_type: 'incoming', private: true, sender_id: 999,
                 content_type: 'input_select', content_attributes: { bot_turn_key: 'spoof', keep: 'synthetic' })
result = commit(successful, values)
created = result.response.fetch(:json)
verify(created[:outcome] == 'committed' && created[:id] == successful.rows.last.id, 'native builder result was not returned')
verify(successful.custom_attributes['unrelated'] == 'preserve' && successful.custom_attributes['workflow_state'] == 'synthetic', 'workflow merge lost unrelated attributes')
built = Messages::MessageBuilder.calls.last.last
verify(built['message_type'] == 'outgoing' && built['private'] == false && !built.key?('sender_id'), 'caller overrode native outgoing authority')
verify(built['content_attributes'] == { 'bot_turn_key' => 'message:10', 'keep' => 'synthetic' }, 'turn key did not replace spoofed key')
verify(built['content_type'] == 'input_select', 'native message type payload changed')
verify(successful.updated_at != Time.utc(2026, 1, 1, 0, 0, 0, 123456), 'workflow version was not advanced')
report['conditional_commit_preserves_native_builder_contract'] = true

[1_700_000_000_123, nil, -1, true, 10**20].each do |received_at|
  timed = Conversation.new
  timed.rows.first.content_attributes['nevidimy_received_at_ms'] = received_at
  values = payload(timed, {}, content_attributes: { nevidimy_received_at_ms: 1, keep: 'synthetic' })
  verify(commit(timed, values).response[:json][:outcome] == 'committed', 'timestamp metadata changed native commit')
  attributes = Messages::MessageBuilder.calls.last.last['content_attributes']
  if received_at == 1_700_000_000_123
    verify(attributes['nevidimy_received_at_ms'] == received_at, 'trusted incoming timestamp was not copied')
  else
    verify(!attributes.key?('nevidimy_received_at_ms'), 'caller spoofed outgoing source timestamp')
  end
  verify(attributes['keep'] == 'synthetic', 'timestamp filtering removed unrelated metadata')
end
report['only_trusted_incoming_timestamp_is_copied'] = true

successful.status = 'resolved'
successful.assignee_id = 9
before_writes, before_rows = successful.writes, successful.rows.size
duplicate = commit(successful, payload(successful, version: 'stale')).response.fetch(:json)
verify(duplicate == { id: created[:id], outcome: 'duplicate' }, 'duplicate was not idempotent before stale/eligibility checks')
verify(successful.writes == before_writes && successful.rows.size == before_rows, 'duplicate partially mutated conversation')
report['duplicate_delivery_is_idempotent'] = true

encoded = Conversation.new
decoded = message(14, :outgoing, false, 'AgentBot', 'message:10')
# The production store coder exposes a Hash even though the raw jsonb kind is
# string. The relation deliberately has no raw JSON-key predicate support.
decoded.raw_content_attributes = JSON.generate(decoded.content_attributes)
decoded.define_singleton_method(:content_attributes) { JSON.parse(raw_content_attributes) }
encoded.rows.concat([
  message(9, :outgoing, false, 'AgentBot', 'message:10'),
  message(11, :outgoing, true, 'AgentBot', 'message:10'),
  message(12, :incoming, false, 'Contact', 'message:10'),
  message(13, :outgoing, false, 'AgentBot', 'message:other'),
  decoded
])
verify(NevidimyAtomicTurn.reply(encoded, 10) == 14, 'encoded store reply was missed or wrong turn matched')
decoded.raw_content_attributes = 'null'
verify(NevidimyAtomicTurn.reply(encoded, 10).nil?, 'non-object decoded metadata matched a turn')
report['serialized_store_reply_lookup_is_scoped_and_decoded'] = true

failed = Conversation.new
before_attributes, before_version = failed.custom_attributes.dup, failed.updated_at
Messages::MessageBuilder.fail = true
begin
  commit(failed)
  raise 'builder error was swallowed'
rescue IOError => error
  verify(error.message == 'synthetic builder failure', 'native builder error changed')
  verify(failed.custom_attributes == before_attributes && failed.updated_at == before_version && failed.rows.size == 1, 'modeled transaction retained partial workflow writes')
ensure
  Messages::MessageBuilder.fail = false
end
report['native_builder_error_propagates_and_modeled_transaction_rolls_back'] = true

def private_note(key = "scenario:#{'a' * 24}", content = 'synthetic note')
  { content: content, event_key: key }
end

noted = Conversation.new
new_state = {
  'scenario_last_message_id' => 10,
  'scenario_requests' => { 'synthetic' => { 'status' => 'pending' } },
  'scenario_event_receipts' => ["scenario:#{'a' * 24}"]
}
notes = [private_note, private_note("scenario-alert:#{'b' * 24}", 'synthetic alert')]
before_calls = Messages::MessageBuilder.calls.size
noted_values = payload(noted, attributes: new_state, notes: notes)
noted_result = commit(noted, noted_values).response.fetch(:json)
built = Messages::MessageBuilder.calls.drop(before_calls)
verify(built.size == 3 && built.all? { |user, record, _values| user.equal?(Current.user) && record.equal?(noted) },
       'notes bypassed bound native AgentBot builder')
verify(built.map { |_user, _record, values| values['private'] } == [true, true, false],
       'private notes were not buffered before the public reply')
built.first(2).each_with_index do |(_user, _record, values), index|
  verify(values == { 'content' => notes[index][:content], 'content_type' => 'text',
                     'message_type' => 'outgoing', 'private' => true,
                     'content_attributes' => { 'bot_event_key' => notes[index][:event_key] } },
         'note payload accepted role, attachment or public-turn injection')
end
verify(noted_result == { id: noted.rows.last.id, outcome: 'committed' }, 'note turn returned a private note id')
verify(noted.custom_attributes['scenario_requests'] == new_state['scenario_requests'] &&
       noted.custom_attributes['scenario_event_receipts'] == new_state['scenario_event_receipts'] &&
       noted.custom_attributes['unrelated'] == 'preserve', 'local-effect state was not merged')
report['private_notes_use_native_builder_and_precede_one_public_reply'] = true

# A lost acknowledgement retries the complete request, including its old version.
# Duplicate public-turn detection must precede state/ownership/notes writes.
noted.status, noted.assignee_id = 'resolved', 9
before_snapshot = Marshal.dump([noted.custom_attributes, noted.updated_at, noted.rows])
before_writes, before_calls = noted.writes, Messages::MessageBuilder.calls.size
ack = commit(noted, noted_values).response.fetch(:json)
verify(ack == { id: noted_result[:id], outcome: 'duplicate' }, 'retry did not acknowledge the existing public reply')
verify(Marshal.dump([noted.custom_attributes, noted.updated_at, noted.rows]) == before_snapshot &&
       noted.writes == before_writes && Messages::MessageBuilder.calls.size == before_calls,
       'duplicate acknowledgement changed notes or workflow')
report['private_note_turn_retry_is_no_write_and_returns_public_id'] = true

deduped = Conversation.new
old_note = message(9, :outgoing, true, 'User')
old_note.raw_content_attributes = JSON.generate('bot_event_key' => private_note[:event_key])
old_note.define_singleton_method(:content_attributes) { JSON.parse(raw_content_attributes) }
deduped.rows << old_note
deduped.rows << message(8, :outgoing, true, 'AgentBot').tap { |row| row.content_attributes = nil }
next_key = "scenario:#{'c' * 24}"
batch = [private_note, private_note(next_key), private_note(next_key, 'synthetic retry')]
before_calls = Messages::MessageBuilder.calls.size
verify(commit(deduped, payload(deduped, notes: batch)).response[:json][:outcome] == 'committed',
       'existing private note blocked the public turn')
created_notes = Messages::MessageBuilder.calls.drop(before_calls).map(&:last)
verify(created_notes.size == 2 && created_notes.first['private'] &&
       created_notes.first['content_attributes'] == { 'bot_event_key' => next_key } &&
       created_notes.last['private'] == false, 'old decoded or duplicate batch event key created another note')
other = Conversation.new
before_calls = Messages::MessageBuilder.calls.size
commit(other, payload(other, notes: [private_note]))
verify(Messages::MessageBuilder.calls.size == before_calls + 2, 'note dedup escaped its conversation')
report['note_dedup_uses_scoped_native_coder_and_no_input_cutoff'] = true

invalid_notes = [
  nil, {}, 'invalid', [nil], [true], ['invalid'], [private_note] * 5,
  [{ content: 'synthetic' }], [{ event_key: private_note[:event_key] }],
  [private_note.merge(role: 'user')], [private_note.merge(private: false)],
  [private_note.merge(sender_type: 'User')], [private_note.merge(sender_id: 99)],
  [private_note.merge(attachments: ['synthetic'])], [private_note.merge(content_type: 'text')],
  [private_note.merge(content_attributes: { bot_turn_key: 'message:10' })],
  [private_note.merge(unknown: true)], [private_note.merge(content: nil)],
  [private_note.merge(content: 1)], [private_note.merge(content: 'x' * 32_769)],
  [private_note.merge(content: "\u00e9" * 16_385)],
  [private_note.merge(content: "\xff".force_encoding(Encoding::UTF_8))],
  [private_note.merge(content: 'synthetic'.encode(Encoding::UTF_16LE))],
  [private_note.merge(event_key: nil)], [private_note.merge(event_key: 1)],
  [private_note.merge(event_key: '')], [private_note.merge(event_key: 'x' * 257)],
  [private_note.merge(event_key: "scenario:#{'A' * 24}")],
  [private_note.merge(event_key: "scenario:#{'a' * 23}")],
  [private_note.merge(event_key: "legacy:#{'a' * 24}")],
  [private_note.merge(event_key: "\xff".force_encoding(Encoding::UTF_8))],
  [private_note.merge(event_key: private_note[:event_key].encode(Encoding::UTF_16LE))],
  [private_note("scenario:#{'a' * 24}", 'x' * 21_846),
   private_note("scenario:#{'b' * 24}", 'x' * 21_846),
   private_note("scenario:#{'c' * 24}", 'x' * 21_846)]
]
invalid_notes.each do |batch|
  invalid = Conversation.new
  before_calls = Messages::MessageBuilder.calls.size
  rejected = commit(invalid, payload(invalid, notes: batch))
  verify(rejected.response[:status] == :conflict && unchanged?(invalid) && invalid.lock_count.zero? &&
         Messages::MessageBuilder.calls.size == before_calls, 'invalid note plan wrote or reached the lock')
end
boundary = Conversation.new
full = "\u00e9" * 16_384
verify(commit(boundary, payload(boundary, notes: [private_note("scenario:#{'d' * 24}", full),
                                               private_note("scenario:#{'e' * 24}", full)])).response[:status] == :ok,
       'valid UTF-8 byte boundaries were rejected')
empty = Conversation.new
verify(commit(empty, payload(empty, notes: [])).response[:status] == :ok && empty.rows.size == 2,
       'empty optional notes changed the public-only contract')
four = Conversation.new
verify(commit(four, payload(four, notes: %w[a b c d].map { |char| private_note("scenario:#{char * 24}") })).response[:status] == :ok &&
       four.rows.size == 6, 'maximum supported note count was rejected')
report['notes_are_fully_validated_before_writes_with_byte_and_role_limits'] = true

staff_conflict = Conversation.new
staff_conflict.rows << message(11, :outgoing, false, 'User')
before_calls = Messages::MessageBuilder.calls.size
rejected = commit(staff_conflict, payload(staff_conflict, notes: notes))
verify(rejected.response[:status] == :conflict && staff_conflict.writes.zero? && staff_conflict.rows.size == 2 &&
       Messages::MessageBuilder.calls.size == before_calls, 'notes bypassed public staff precedence')
report['private_note_turn_keeps_staff_precedence_without_writes'] = true

[2, 3].each do |failure_offset|
  failed = Conversation.new
  before_snapshot = Marshal.dump([failed.custom_attributes, failed.updated_at, failed.rows])
  before_calls = Messages::MessageBuilder.calls.size
  Messages::MessageBuilder.fail_at = before_calls + failure_offset
  begin
    commit(failed, payload(failed, attributes: new_state, notes: notes))
    raise 'note-turn builder failure was swallowed'
  rescue IOError => error
    verify(error.message == 'synthetic builder failure', 'note-turn builder exception changed')
    verify(Marshal.dump([failed.custom_attributes, failed.updated_at, failed.rows]) == before_snapshot,
           'second-note or public-reply failure retained partial messages or local-effect state')
    verify(Messages::MessageBuilder.calls.size == before_calls + failure_offset,
           'note-turn failure retried or continued native builders')
  ensure
    Messages::MessageBuilder.fail_at = nil
  end
end
report['second_note_and_public_reply_failures_roll_back_complete_local_effect_turn'] = true

Conversation.staff_locks.clear
Message.new(true, false, 'User').run_callbacks
verify(Conversation.staff_locks == [[:where, 900], [:lock, true], [:pick, :id]], 'public staff message did not participate in conversation lock')
Conversation.staff_locks.clear
[[false, false, 'User'], [true, true, 'User'], [true, false, 'AgentBot']].each do |arguments|
  Message.new(*arguments).run_callbacks
end
verify(Conversation.staff_locks.empty?, 'irrelevant message acquired staff lock')
report['staff_callback_uses_same_conversation_lock_scope'] = true

puts JSON.generate(report)

# Synthetic metadata only. Native source-digest acceptance is checked separately
# in the pinned image; this probe covers routing, fallback and configuration.
require 'json'
require 'ostruct'
require 'tempfile'

def verify(value, reason)
  raise reason unless value
end

class Capsule
  attr_accessor :queues, :concurrency, :mode

  def initialize(queues, concurrency = 2, mode = :strict)
    @queues, @concurrency, @mode = queues, concurrency, mode
  end
end

class Configuration
  attr_reader :capsules, :hooks, :logger

  def initialize(queues = %w[critical high medium default mailers low], concurrency = 2)
    @capsules = { 'default' => Capsule.new(queues, concurrency) }
    @hooks = {}
    @logger = Object.new
    @logger.define_singleton_method(:info) { |event| @last = JSON.parse(event) }
  end

  def default_capsule
    @capsules.fetch('default')
  end

  def capsule(name)
    yield(@capsules[name] ||= Capsule.new(['default']))
  end

  def total_concurrency
    @capsules.values.sum(&:concurrency)
  end

  def on(event, &block)
    @hooks[event] = block
  end
end

module Sidekiq
  VERSION = '7.3.10'
  CONFIG = Configuration.new

  def self.configure_server
    yield CONFIG
  end
end

module Rails
  RELOADER = Object.new
  RELOADER.define_singleton_method(:to_prepare) { |&block| @prepare = block }
  RELOADER.define_singleton_method(:prepare) { @prepare.call }

  def self.application
    OpenStruct.new(reloader: RELOADER)
  end
end

module Channel
  class Telegram; end
  class Api; end
end

class Message
  attr_accessor :inbox_id, :conversation, :attachments, :error, :content_attributes
  attr_reader :jobs

  def initialize(incoming: false, private_message: false, channel: Channel::Telegram.new)
    @incoming, @private_message = incoming, private_message
    @inbox_id = 5
    @conversation = OpenStruct.new(inbox_id: 5, inbox: OpenStruct.new(channel: channel))
    @attachments, @jobs = [], []
    @content_attributes = {}
  end

  def incoming?
    @incoming
  end

  def private?
    @private_message
  end

  def send_reply
    raise @error if @error
    @jobs << (attachments.empty? ? :immediate : :delayed)
    :native_result
  end
end

class SendReplyJob
  def perform; end
end

module Base
  class SendOnChannelService
    def perform; end
  end
end

module Telegram
  class SendOnTelegramService
    def perform_reply; end
  end
end

load File.expand_path('../deploy/timeweb/interactive_jobs.rb', __dir__)
report = {}
config = Sidekiq::CONFIG
verify(config.total_concurrency == 2, 'worker count increased')
verify(config.default_capsule.queues == %w[critical medium default mailers low], 'background order changed')
verify(config.default_capsule.concurrency == 1, 'background worker lost')
interactive = config.capsules.fetch(NevidimyInteractiveJobs::CAPSULE)
verify(interactive.queues == ['high'] && interactive.concurrency == 1, 'delivery not isolated')
report['existing_native_queues_split_without_extra_workers'] = true

before = config.capsules.values.map { |cap| [cap.queues.dup, cap.concurrency] }
verify(!NevidimyInteractiveJobs.isolate(config), 'repeat installation changed capsules')
verify(before == config.capsules.values.map { |cap| [cap.queues, cap.concurrency] }, 'repeat installation was not safe')
report['capsule_installation_is_idempotent'] = true

candidates = [Configuration.new(['default']), Configuration.new(%w[high default], 1),
              Configuration.new(%w[high default], 3), Configuration.new(%w[high high default])]
weighted = Configuration.new
weighted.default_capsule.mode = :weighted
candidates << weighted
extra = Configuration.new
extra.capsule('other') { |cap| cap.concurrency = 1 }
candidates << extra
candidates.each do |candidate|
  baseline = Marshal.dump(candidate.capsules)
  verify(!NevidimyInteractiveJobs.isolate(candidate), 'unsupported configuration changed')
  verify(Marshal.dump(candidate.capsules) == baseline, 'fallback changed queues')
end
Sidekiq.send(:remove_const, :VERSION)
Sidekiq.const_set(:VERSION, '8.0.0')
verify(!NevidimyInteractiveJobs.isolate(Configuration.new), 'unknown Sidekiq accepted')
Sidekiq.send(:remove_const, :VERSION)
Sidekiq.const_set(:VERSION, '7.3.10')
report['unknown_or_custom_configuration_retains_native_processing'] = true

# The pinned sidekiq_alive gem registers its own two-thread capsule in an
# earlier startup hook, after application initializers configured the workers.
config.capsule('sidekiq-alive') do |health|
  health.queues = ['sidekiq-alive']
  health.concurrency = 2
end
config.hooks.fetch(:startup).call
event = config.logger.instance_variable_get(:@last)
verify(event.keys.sort == %w[background_concurrency concurrency event interactive_concurrency isolated pid], 'unsafe startup metadata')
verify(event['isolated'] && event['concurrency'] == 4 && event['background_concurrency'] == 1 &&
       event['interactive_concurrency'] == 1 && event['pid'] == Process.pid, 'invalid startup proof')
verify(config.capsules.fetch('sidekiq-alive').concurrency == 2, 'native health workers changed')
report['startup_reports_actual_process_configuration_without_payloads'] = true

Rails::RELOADER.prepare
verify(!(Message < NevidimyInteractiveJobs::MessageDelivery), 'unknown native source installed')
Tempfile.create(['native-job', '.rb']) do |file|
  source = "class SyntheticNativeJob\n  def perform; end\nend\n"
  file.write(source)
  file.flush
  load file.path
  digest = Digest::SHA256.hexdigest(source)
  verify(NevidimyInteractiveJobs.native_file?(SyntheticNativeJob, :perform, digest), 'known source rejected')
  verify(NevidimyInteractiveJobs.native_file?(SyntheticNativeJob, :perform, ['unknown', digest]), 'second approved source rejected')
  verify(NevidimyInteractiveJobs.native_file?(SyntheticNativeJob, :perform, [digest, 'unknown']), 'first approved source rejected')
  verify(!NevidimyInteractiveJobs.native_file?(SyntheticNativeJob, :perform, %w[unknown other]), 'unknown sources accepted')
  verify(!NevidimyInteractiveJobs.native_file?(SyntheticNativeJob, :perform, 'unknown'), 'changed source accepted')
  SyntheticNativeJob.prepend(Module.new { def perform; super; end })
  verify(!NevidimyInteractiveJobs.native_file?(SyntheticNativeJob, :perform, [digest]), 'unknown override accepted')
end
Message.prepend(NevidimyInteractiveJobs::MessageDelivery)
NevidimyInteractiveJobs.install
verify(Message.ancestors.count(NevidimyInteractiveJobs::MessageDelivery) == 1, 'duplicate delivery guard')
report['source_and_owner_guards_fail_closed_and_reload_is_safe'] = true

[Message.new(incoming: true), Message.new(private_message: true),
 Message.new(incoming: true, private_message: true)].each do |row|
  original = [row.inbox_id, row.conversation, row.attachments]
  verify(row.send(:send_reply).nil? && row.jobs.empty?, 'proven no-op still enqueued')
  verify(original == [row.inbox_id, row.conversation, row.attachments], 'row changed')
end
report['telegram_incoming_and_private_rows_omit_only_the_noop_delivery_job'] = true

[true, 0, '', 'false', [], {}].each do |pending|
  [Message.new(incoming: true), Message.new(private_message: true),
   Message.new(incoming: true, private_message: true)].each do |row|
    row.content_attributes['translation_pending'] = pending
    verify(row.send(:send_reply) == :native_result && row.jobs == [:immediate], 'truthy pending translation lost native job')
    verify(row.content_attributes['translation_pending'].equal?(pending), 'pending translation changed')
  end
end
[nil, false].each do |pending|
  row = Message.new(incoming: true)
  row.content_attributes['translation_pending'] = pending
  verify(row.send(:send_reply).nil? && row.jobs.empty?, 'falsey translation prevented proven no-op skip')
end
report['translation_pending_uses_native_ruby_truthiness_and_preserves_delivery'] = true

[Message.new, Message.new(incoming: true, channel: Channel::Api.new),
 Message.new(private_message: true, channel: Class.new(Channel::Telegram).new)].each do |row|
  verify(row.send(:send_reply) == :native_result && row.jobs == [:immediate], 'public or unknown channel lost native delivery')
end
attachment = Message.new
attachment.attachments << Object.new
verify(attachment.send(:send_reply) == :native_result && attachment.jobs == [:delayed], 'attachment scheduling changed')
failure = Message.new
failure.error = RuntimeError.new('native_failure')
begin
  failure.send(:send_reply)
  raise 'native exception swallowed'
rescue RuntimeError => error
  verify(error.equal?(failure.error), 'native exception changed')
end
report['public_delivery_attachments_return_values_and_errors_remain_native'] = true

unknown = Message.new(incoming: true)
unknown.conversation.inbox_id = 6
verify(unknown.send(:send_reply) == :native_result, 'mismatched inbox skipped')
unknown = Message.new(incoming: true)
unknown.inbox_id = nil
unknown.conversation.inbox_id = nil
verify(unknown.send(:send_reply) == :native_result, 'unknown inbox skipped')
unknown = Message.new(incoming: true)
unknown.conversation = nil
verify(unknown.send(:send_reply) == :native_result, 'association error skipped native job')
[nil, '', []].each do |attributes|
  unknown = Message.new(incoming: true)
  unknown.content_attributes = attributes
  verify(unknown.send(:send_reply) == :native_result, 'unknown content attributes skipped native job')
end
report['ambiguous_metadata_falls_back_instead_of_dropping_work'] = true
puts JSON.generate(report)

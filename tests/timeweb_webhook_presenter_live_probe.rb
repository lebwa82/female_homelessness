# Read-only native serializer comparison in an already validated synthetic API
# conversation. This is not a Telegram-delivery or end-to-end latency benchmark.
require 'json'
require 'logger'

begin
  quiet = Logger.new(File::NULL)
  Rails.logger = ActiveRecord::Base.logger = quiet
  config = JSON.parse(ENV.fetch('NEVIDIMY_PRESENTER_PROBE'))
  raise 'invalid_config' unless config.keys.sort == %w[account_id conversation_id inbox_id run_id]

  account = Account.find(config.fetch('account_id'))
  inbox = account.inboxes.find(config.fetch('inbox_id'))
  raise 'not_isolated_api' unless inbox.channel_type == 'Channel::Api' &&
                                 inbox.name.start_with?('Nevidimy atomic turn verification ')

  [inbox, inbox.channel].each do |record|
    raise 'external_callback' unless record.attributes.select { |key, _| key.match?(/(?:webhook|callback).*url/) }.values.all?(&:blank?)
  end
  conversation = account.conversations.find_by!(display_id: config.fetch('conversation_id'), inbox_id: inbox.id)
  raise 'wrong_run' unless conversation.custom_attributes['acceptance_run_id'] == config.fetch('run_id')

  presenter = Conversations::EventDataPresenter
  native_webhook = presenter.instance_method(:webhook_data)
  require '/run/nevidimy-check/webhook_presenter_fast_path.rb'
  Rails.application.reloader.prepare!
  raise 'optimization_disabled' unless presenter < NevidimyWebhookPresenterFastPath::Presenter

  measure = lambda do |optimized|
    counts = { schema: 0, cached: 0, uncached: 0 }
    subscriber = ActiveSupport::Notifications.subscribe('sql.active_record') do |_, _, _, _, payload|
      kind = payload[:name] == 'SCHEMA' ? :schema : (payload[:cached] ? :cached : :uncached)
      counts[kind] += 1
    end
    ActiveRecord::Base.connection.clear_query_cache
    before = GC.stat(:total_allocated_objects)
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    result = ActiveRecord::Base.cache do
      fresh = account.conversations.find(conversation.id)
      view = presenter.new(fresh)
      optimized ? view.webhook_data : native_webhook.bind(view).call
    end
    elapsed = (Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000
    [result, { elapsed_ms: elapsed.round(3), allocated_objects: GC.stat(:total_allocated_objects) - before, sql: counts }]
  ensure
    ActiveSupport::Notifications.unsubscribe(subscriber) if subscriber
  end

  baseline, = measure.call(false)
  candidate, = measure.call(true)
  raise 'payload_changed' unless baseline == candidate
  samples = { baseline: [], candidate: [] }
  12.times do |index|
    # Alternate first position to avoid attributing warm-up/order to the patch.
    modes = index.even? ? [false, true] : [true, false]
    modes.each do |optimized|
      result, sample = measure.call(optimized)
      raise 'payload_drift' unless result == baseline

      samples[optimized ? :candidate : :baseline] << sample
    end
  end
  raise 'scope_leaked' unless Thread.current[NevidimyWebhookPresenterFastPath::SCOPE_KEY].nil?

  puts JSON.generate(ok: true, payload_equal: true, read_only: true, samples: samples)
rescue StandardError => error
  puts JSON.generate(ok: false, error_type: error.class.name)
  exit 1
end

# WebhookJob arguments include signing credentials and conversation payloads.
Rails.application.reloader.to_prepare do
  WebhookJob.log_arguments = false
end

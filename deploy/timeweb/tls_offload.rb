# Only Caddy is published; Rails and its API clients share the private Docker
# network. Keep native FORCE_SSL secure cookies without redirecting internal
# bot API calls to an unpublished HTTPS listener on the Rails container.
Rails.application.config.assume_ssl = true

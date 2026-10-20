# Chatwoot HTTPS by IP

Append `compose.ip-https.yml` to the base, managed-PostgreSQL and Timeweb files;
do not append `compose.http-local.yml`. Set `CHATWOOT_PUBLIC_IP` in the root-only
`/etc/women-help-chatwoot.env` to the VM's current API-verified IPv4. The entry
point is `https://<IPv4>/app/login`; port 80 only redirects and serves ACME.
Only Caddy publishes ports. The Agent Bot, Rails, PostgreSQL and Redis are not
published by this override. Rails assumes TLS offload by Caddy so its native
secure cookies and internal HTTP API clients both continue to work.

Install `ip-https.conf` under `/etc/systemd/system/nevidimy.service.d/`, then
reload systemd. During a live transition, use Compose `up --no-deps` only for
`caddy`, then `chatwoot sidekiq`; do not restart the core systemd unit, because
that would also stop dependent Telegram/Agent units. Preserve volumes, images,
credentials and all Telegram configuration. Validate Compose and Caddy first.

Let's Encrypt IP certificates use the `shortlived` profile (about six days).
Caddy renews them automatically; keep it running with the existing persistent
`caddy_data` and `caddy_config` volumes. After installation, verify the public
certificate chain and IP SAN, HTTP redirect, login and internal API access.
The old localhost tunnel URL is replaced, not made public.

References: [Let's Encrypt IP certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability),
[Caddy ACME issuer](https://caddyserver.com/docs/caddyfile/directives/tls),
[Rails TLS offload](https://api.rubyonrails.org/classes/ActionDispatch/SSL.html).

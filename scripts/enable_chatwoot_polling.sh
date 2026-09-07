#!/usr/bin/env bash
# Run after deploying a release containing telegram-ingress. No secret output.
set -euo pipefail
host="$(uv run python -m scripts.resolve_prod_host --ip-only)"
uv run python -m scripts.resolve_prod_host --verify-ssh "$host" >/dev/null
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 \
  -l lebwa82 "$host" 'bash -s' <<'REMOTE_SCRIPT'
set -euo pipefail
cd /opt/women-help-chatwoot
if sudo systemctl is-active --quiet women-help-bot.service; then
  echo 'Stop the legacy women-help-bot service before enabling the new ingress.' >&2
  exit 1
fi
sudo python3 deploy/chatwoot/activate.py enable_polling
sudo install -m 0644 deploy/chatwoot/women-help-telegram-ingress.service \
  /etc/systemd/system/women-help-telegram-ingress.service
sudo systemctl daemon-reload
sudo systemctl stop women-help-telegram-ingress.service
sudo systemctl enable --now women-help-telegram-ingress.service
for attempt in {1..30}; do
  if sudo podman exec women-help-chatwoot_telegram-ingress_1 \
    /app/.venv/bin/python -m app.chatwoot.telegram_ingress --check; then
    echo 'Telegram polling: healthy'
    exit 0
  fi
  sleep 3
done
echo 'Telegram polling did not become healthy.' >&2
exit 1
REMOTE_SCRIPT

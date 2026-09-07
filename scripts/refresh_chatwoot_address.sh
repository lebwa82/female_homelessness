#!/usr/bin/env bash
# Query the cloud on each invocation and repair generated test hostnames.
set -euo pipefail
host="$(uv run python -m scripts.resolve_prod_host --ip-only)"
result="$(ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 \
  -l lebwa82 "$host" "sudo python3 - '$host'" < scripts/update_chatwoot_address.py)"
changed="${result%%$'\n'*}"
url="${result#*$'\n'}"
if [[ "$changed" == "changed" ]]; then
  ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 \
    -l lebwa82 "$host" 'bash -s' <<'REMOTE_SCRIPT'
set -euo pipefail
agent_active=0
if sudo systemctl is-active --quiet women-help-chatwoot-agent; then agent_active=1; fi
sudo systemctl restart women-help-chatwoot.service </dev/null
if [[ "$agent_active" == 1 ]]; then sudo systemctl restart women-help-chatwoot-agent.service </dev/null; fi
REMOTE_SCRIPT
fi
printf 'Chatwoot: %s\n' "$url"
ready=0
for attempt in {1..30}; do
  if curl -fsSL --max-time 5 -o /dev/null "$url" 2>/dev/null; then
    echo 'HTTPS check: OK'
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  echo 'HTTPS check failed; inspect just chatwoot-check.' >&2
  exit 1
fi
# Always reconcile persisted routes, including a retry after a partial refresh.
# Execute the local helper so the repair also works on older deployed releases.
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 \
  -l lebwa82 "$host" 'sudo python3 - refresh_routes' < deploy/chatwoot/activate.py
echo 'Chatwoot and Telegram routes: OK'

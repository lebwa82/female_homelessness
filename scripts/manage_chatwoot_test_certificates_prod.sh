#!/usr/bin/env bash
# Run a narrowly scoped test-certificate reset or purge inside Agent Bot.
set -euo pipefail

operation="${1:-}"
host="${2:-}"
case "$operation" in
  reset) module=scripts.reset_test_certificates ;;
  purge) module=scripts.purge_test_certificates ;;
  *) echo "Usage: $0 reset|purge [HOST_IP]" >&2; exit 2 ;;
esac
if [[ -z "$host" ]]; then host="$(uv run python -m scripts.resolve_prod_host --ip-only)"; fi
uv run python -m scripts.resolve_prod_host --verify-ssh "$host" >/dev/null
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -l lebwa82 "$host" \
  "MODULE='${module}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
container="$(sudo podman ps --filter label=io.podman.compose.project=women-help-chatwoot --filter label=com.docker.compose.service=agent-bot --format '{{.ID}}')"
if [[ "$(wc -w <<<"$container")" != 1 ]]; then
  echo "Expected one running agent-bot container." >&2
  exit 3
fi
sudo podman exec "$container" /app/.venv/bin/python -m "$MODULE"
REMOTE_SCRIPT

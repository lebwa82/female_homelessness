#!/usr/bin/env bash
# Upload a certificate batch transiently, import it inside Agent Bot, then remove it.
set -euo pipefail

json_file="${1:-}"
host="${2:-}"
operation="${3:-import}"
if [[ "$operation" != "import" && "$operation" != "link" ]]; then
  echo "Unsupported certificate operation." >&2
  exit 2
fi
if [[ ! -f "$json_file" ]]; then
  echo "Usage: $0 CERTIFICATES.json [HOST_IP]" >&2
  exit 2
fi
if [[ -z "$host" ]]; then host="$(uv run python -m scripts.resolve_prod_host --ip-only)"; fi
if ! [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "The production host must be an IPv4 address." >&2
  exit 2
fi

uv run python -m scripts.resolve_prod_host --verify-ssh "$host" >/dev/null
remote_file="/tmp/women-help-certificates-$$.json"
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -l lebwa82 "$host" \
  "umask 077; cat > '${remote_file}'" <"$json_file"
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -l lebwa82 "$host" \
  "REMOTE_FILE='${remote_file}' OPERATION='${operation}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
container="$(sudo podman ps \
  --filter label=io.podman.compose.project=women-help-chatwoot \
  --filter label=com.docker.compose.service=agent-bot \
  --format '{{.ID}}')"
if [[ "$(wc -w <<<"$container")" != 1 ]]; then
  sudo rm -f "$REMOTE_FILE"
  echo "Expected one running agent-bot container." >&2
  exit 3
fi
container_file=/tmp/women-help-certificates.json
cleanup() {
  sudo podman exec "$container" rm -f "$container_file" >/dev/null 2>&1 || true
  sudo rm -f "$REMOTE_FILE"
}
trap cleanup EXIT
sudo podman cp "$REMOTE_FILE" "${container}:${container_file}" >/dev/null
if [[ "$OPERATION" == "link" ]]; then
  module=scripts.link_legacy_certificates
else
  module=scripts.import_chatwoot_certificates
fi
sudo podman exec "$container" /app/.venv/bin/python -m "$module" "$container_file"
REMOTE_SCRIPT

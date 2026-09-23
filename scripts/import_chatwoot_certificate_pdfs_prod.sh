#!/usr/bin/env bash
# Transfer PDFs transiently and import them from the running Agent Bot container.
set -euo pipefail

directory="${1:-}"
host="${2:-}"
if [[ ! -d "$directory" ]]; then
  echo "Usage: $0 PDF_DIRECTORY [HOST_IP]" >&2
  exit 2
fi
if [[ -z "$host" ]]; then host="$(uv run python -m scripts.resolve_prod_host --ip-only)"; fi
if ! [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "The production host must be an IPv4 address." >&2
  exit 2
fi
uv run python -m scripts.resolve_prod_host --verify-ssh "$host" >/dev/null

archive="$(mktemp -t women-help-certificates.XXXXXX.tar)"
remote_file="/tmp/women-help-certificates-$$.tar"
cleanup() { rm -f "$archive"; }
trap cleanup EXIT
(cd "$directory" && tar -cf "$archive" -- *.pdf)
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -l lebwa82 "$host" \
  "umask 077; cat > '${remote_file}'" <"$archive"
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -l lebwa82 "$host" \
  "REMOTE_FILE='${remote_file}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
container="$(sudo podman ps --filter label=io.podman.compose.project=women-help-chatwoot --filter label=com.docker.compose.service=agent-bot --format '{{.ID}}')"
if [[ "$(wc -w <<<"$container")" != 1 ]]; then
  sudo rm -f "$REMOTE_FILE"
  echo "Expected one running agent-bot container." >&2
  exit 3
fi
remote_dir="/tmp/women-help-certificate-pdfs"
container_dir="/tmp/women-help-certificate-pdfs"
cleanup() {
  sudo podman exec "$container" rm -rf "$container_dir" >/dev/null 2>&1 || true
  sudo rm -rf "$remote_dir" "$REMOTE_FILE"
}
trap cleanup EXIT
sudo mkdir -m 0700 "$remote_dir"
sudo tar -C "$remote_dir" -xf "$REMOTE_FILE"
sudo podman cp "$remote_dir" "${container}:${container_dir}" >/dev/null
sudo podman exec "$container" /app/.venv/bin/python -m scripts.import_chatwoot_certificate_pdfs "$container_dir"
REMOTE_SCRIPT

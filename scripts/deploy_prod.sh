#!/usr/bin/env bash
# Deploy the current committed repository snapshot to one MVP VM.
set -euo pipefail

host="${1:-}"
target_dir="${2:-/opt/women-help-bot}"

if [[ -z "$host" ]]; then
  host="$(uv run python -m scripts.resolve_prod_host)"
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to deploy: commit or stash local changes first." >&2
  exit 2
fi

revision="$(git rev-parse --short HEAD)"
archive_path="/tmp/women-help-${revision}.tar"

echo "Deploying ${revision} to ${host}:${target_dir}"
git archive --format=tar HEAD |
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" "cat > '${archive_path}'"

ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" \
  "TARGET_DIR='${target_dir}' REVISION='${revision}' ARCHIVE_PATH='${archive_path}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
trap 'rm -f "$ARCHIVE_PATH"' EXIT

sudo mkdir -p "$TARGET_DIR"
sudo tar -xf "$ARCHIVE_PATH" -C "$TARGET_DIR"

# Keep secrets in the existing root-only EnvironmentFile. Change only
# non-secret deployment metadata, without displaying the file.
sudo sed -i '/^APP_ENV=/d; /^ENV=/d; /^BUILD_VERSION=/d' /etc/women-help-bot.env
printf '\nAPP_ENV=production\nBUILD_VERSION=%s\n' "$REVISION" |
  sudo tee -a /etc/women-help-bot.env >/dev/null

cd "$TARGET_DIR"
sudo uv sync --all-groups --locked
sudo systemctl restart women-help-bot
sleep 2
sudo systemctl is-active --quiet women-help-bot
sudo just check
sudo journalctl -u women-help-bot -n 12 --no-pager -o cat
REMOTE_SCRIPT

echo "Deployment ${revision} completed."

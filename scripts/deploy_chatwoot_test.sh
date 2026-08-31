#!/usr/bin/env bash
# Deploy the Chatwoot test contour without exposing credentials. It starts the
# dashboard first; the Agent Bot stays disabled until its Chatwoot tokens and
# duty-team ID have been configured in /etc/women-help-agent.env.
set -euo pipefail

host="${1:-84.252.139.95}"
target_dir="${2:-/opt/women-help-chatwoot}"
revision="$(git rev-parse --short HEAD)"
archive_path="/tmp/women-help-chatwoot-${revision}.tar"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to deploy: commit or stash local changes first." >&2
  exit 2
fi

if ! [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "The test deployment host must be an IPv4 address." >&2
  exit 2
fi
if [[ "$target_dir" != "/opt/women-help-chatwoot" ]]; then
  echo "The Chatwoot test deployment path is fixed to /opt/women-help-chatwoot." >&2
  exit 2
fi

echo "Staging Chatwoot test release ${revision} on ${host}."
git archive --format=tar HEAD |
  ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 -l lebwa82 "$host" \
    "cat > '${archive_path}'"

ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 -l lebwa82 "$host" \
  "TARGET_DIR='${target_dir}' REVISION='${revision}' ARCHIVE_PATH='${archive_path}' HOST_IP='${host}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

readonly CHATWOOT_ENV=/etc/women-help-chatwoot.env
readonly AGENT_ENV=/etc/women-help-agent.env
readonly RELEASE_ROOT="${TARGET_DIR}.releases"
readonly RELEASE_DIR="${RELEASE_ROOT}/${REVISION}"
readonly STAGING_DIR="${RELEASE_ROOT}/.${REVISION}.staging.$$"

sudo -n true

if ! command -v podman >/dev/null 2>&1 || ! sudo podman compose version >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Podman Compose is missing and this VM has no supported package manager." >&2
    exit 3
  fi
  sudo apt-get update
  sudo apt-get install -y podman podman-compose
fi

if [[ ! -s "$CHATWOOT_ENV" ]]; then
  safe_ip="${HOST_IP//./-}"
  postgres_password="$(openssl rand -hex 32)"
  secret_key_base="$(openssl rand -hex 48)"
  webhook_secret="$(openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n')"
  sudo install -d -m 0755 /etc
  sudo /usr/bin/tee "$CHATWOOT_ENV" >/dev/null <<ENVIRONMENT
CHATWOOT_HOSTNAME=chatwoot.${safe_ip}.sslip.io
AGENT_HOSTNAME=agent.${safe_ip}.sslip.io
ENABLE_ACCOUNT_SIGNUP=true
POSTGRES_PASSWORD=${postgres_password}
SECRET_KEY_BASE=${secret_key_base}
CHATWOOT_WEBHOOK_SECRET=${webhook_secret}
CHATWOOT_WEBHOOK_HMAC_SECRET=
ENVIRONMENT
  sudo chmod 0600 "$CHATWOOT_ENV"
  sudo chown root:root "$CHATWOOT_ENV"
fi

# podman-compose 1.x writes generated `podman run` commands to its output.
# Keep every secret in root-only env files and synchronise only the agent's
# route credentials here, without ever printing their values.
agent_env_tmp="$(sudo mktemp /etc/.women-help-agent.env.XXXXXX)"
if [[ -s "$AGENT_ENV" ]]; then
  sudo /usr/bin/awk -F= \
    '$1 != "CHATWOOT_WEBHOOK_SECRET" && $1 != "CHATWOOT_WEBHOOK_HMAC_SECRET" {print $0}' \
    "$AGENT_ENV" | sudo /usr/bin/tee "$agent_env_tmp" >/dev/null
elif [[ -s /etc/women-help-bot.env ]]; then
  sudo /usr/bin/awk -F= '/^(YANDEX_AI_API_KEY|APP_ENV|BUILD_VERSION)=/ {print $0}' \
    /etc/women-help-bot.env | sudo /usr/bin/tee "$agent_env_tmp" >/dev/null
else
  sudo /usr/bin/truncate -s 0 "$agent_env_tmp"
fi
sudo /usr/bin/awk -F= \
  '$1 == "CHATWOOT_WEBHOOK_SECRET" || $1 == "CHATWOOT_WEBHOOK_HMAC_SECRET" {print $0}' \
  "$CHATWOOT_ENV" | sudo /usr/bin/tee -a "$agent_env_tmp" >/dev/null
sudo chmod 0600 "$agent_env_tmp"
sudo chown root:root "$agent_env_tmp"
sudo mv -f "$agent_env_tmp" "$AGENT_ENV"

sudo install -d -m 0755 "$RELEASE_ROOT"
sudo rm -rf "$STAGING_DIR"
sudo install -d -m 0755 "$STAGING_DIR"
sudo tar -xf "$ARCHIVE_PATH" -C "$STAGING_DIR"
sudo rm -f "$ARCHIVE_PATH"
sudo mv "$STAGING_DIR" "$RELEASE_DIR"
sudo ln -sfn "$RELEASE_DIR" "${TARGET_DIR}.next"
sudo mv -Tf "${TARGET_DIR}.next" "$TARGET_DIR"

sudo install -m 0644 "$TARGET_DIR/deploy/chatwoot/women-help-chatwoot.service" \
  /etc/systemd/system/women-help-chatwoot.service
sudo install -m 0644 "$TARGET_DIR/deploy/chatwoot/women-help-chatwoot-agent.service" \
  /etc/systemd/system/women-help-chatwoot-agent.service
sudo systemctl daemon-reload

cd "$TARGET_DIR"
sudo podman compose --env-file "$CHATWOOT_ENV" -f deploy/chatwoot/compose.yml up -d postgres redis
sudo podman compose --env-file "$CHATWOOT_ENV" -f deploy/chatwoot/compose.yml run --rm --no-deps chatwoot \
  bundle exec rails db:chatwoot_prepare
sudo systemctl enable --now women-help-chatwoot.service

chatwoot_hostname="$(sudo /usr/bin/awk -F= '$1 == "CHATWOOT_HOSTNAME" {print $2}' "$CHATWOOT_ENV")"
healthy=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 -H "Host: ${chatwoot_hostname}" \
    http://127.0.0.1/ >/dev/null; then
    healthy=1
    break
  fi
  sleep 2
done

if [[ "$healthy" != 1 ]]; then
  echo "Chatwoot did not become reachable through Caddy." >&2
  exit 4
fi

sudo podman compose --env-file "$CHATWOOT_ENV" -f deploy/chatwoot/compose.yml ps
sudo stat -c 'chatwoot-env mode=%a owner=%U:%G; agent-env=%s' "$CHATWOOT_ENV" "$AGENT_ENV" 2>/dev/null || \
  sudo stat -c 'chatwoot-env mode=%a owner=%U:%G; agent-env=missing' "$CHATWOOT_ENV"

echo "Chatwoot test stack is running at https://${chatwoot_hostname}. Create the first dashboard account, then configure Agent Bot credentials in $AGENT_ENV and enable women-help-chatwoot-agent.service."
REMOTE_SCRIPT

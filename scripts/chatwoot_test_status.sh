#!/usr/bin/env bash
# Read-only status of the Chatwoot test contour. No environment-file values are
# printed, so this command is safe to share in an operator terminal session.
set -euo pipefail

host="${1:-84.252.139.95}"
if ! [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "The test deployment host must be an IPv4 address." >&2
  exit 2
fi

ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 -l lebwa82 "$host" \
  'sudo systemctl is-active women-help-chatwoot.service; sudo systemctl is-active women-help-chatwoot-agent.service || true; sudo podman compose --env-file /etc/women-help-chatwoot.env -f /opt/women-help-chatwoot/current/deploy/chatwoot/compose.yml ps; sudo stat -c "chatwoot-env mode=%a owner=%U:%G" /etc/women-help-chatwoot.env; if test -e /etc/women-help-agent.env; then sudo stat -c "agent-env mode=%a owner=%U:%G" /etc/women-help-agent.env; else echo "agent-env missing"; fi'

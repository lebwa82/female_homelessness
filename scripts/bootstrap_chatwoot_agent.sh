#!/usr/bin/env bash
# Provision the Agent Bot using root-only files on the test VM. The subprocess
# may read credentials, but it emits only public Chatwoot object IDs.
set -euo pipefail

host="${1:-51.250.26.31}"
if ! [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "The test deployment host must be an IPv4 address." >&2
  exit 2
fi

ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10 -l lebwa82 "$host" \
  'bash -s' <<'REMOTE_SCRIPT'
set -euo pipefail

readonly PROJECT=/opt/women-help-chatwoot/current
readonly CHATWOOT_ENV=/etc/women-help-chatwoot.env
readonly AGENT_ENV=/etc/women-help-agent.env

sudo -n true
sudo test -s "$CHATWOOT_ENV"
sudo test -s "$AGENT_ENV"
cd "$PROJECT"

sudo podman compose --env-file "$CHATWOOT_ENV" -f deploy/chatwoot/compose.yml --profile agent build agent-bot
sudo podman compose --env-file "$CHATWOOT_ENV" -f deploy/chatwoot/compose.yml --profile agent run --rm \
  --volume "$AGENT_ENV:/run/women-help-agent.env:rw" \
  --volume "$CHATWOOT_ENV:/run/women-help-chatwoot.env:ro" \
  agent-bot python -m deploy.chatwoot.bootstrap \
    --agent-env /run/women-help-agent.env \
    --chatwoot-env /run/women-help-chatwoot.env
sudo systemctl enable --now women-help-chatwoot-agent.service
REMOTE_SCRIPT

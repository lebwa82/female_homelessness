#!/usr/bin/env bash
# Initial bootstrap of the user-approved Timeweb VM; not a Yandex deployment.
set -euo pipefail

expected_host=9351701-af950022.twc1.net
expected_marker=timeweb-nevidimy-prod-app-msk-1
marker=/etc/nevidimy-deployment-target

[[ $(id -u) == 0 ]]
[[ $(hostname -f) == "$expected_host" ]] || { echo 'Unexpected VM hostname' >&2; exit 1; }
if [[ -e "$marker" && $(<"$marker") != "$expected_marker" ]]; then
  echo 'Conflicting deployment target marker' >&2
  exit 1
fi
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 26.04 ]]

# Do not remove or replace any existing container runtime.
if ! command -v docker >/dev/null; then
  for package in docker-ce podman-docker containerd.io; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
      echo "Existing runtime package requires reconciliation: $package" >&2
      exit 1
    fi
  done
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # The vendor repository is unreachable from this VM. Use Ubuntu's signed,
  # security-updated packages rather than disabling HTTPS verification.
  apt-get install -y --no-install-recommends \
    ca-certificates curl postgresql-client rsync docker.io docker-compose-v2
fi

install -d -m 0755 /etc/docker /opt/nevidimy
install -d -m 0700 /opt/nevidimy/secrets
new_docker_config=false
if [[ ! -e /etc/docker/daemon.json ]]; then
  install -m 0644 /dev/stdin /etc/docker/daemon.json <<'EOF'
{"log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}
EOF
  new_docker_config=true
fi
systemctl enable --now docker
if "$new_docker_config"; then
  systemctl restart docker
fi
# The marker records deployment intent after the user's accepted SSH trust;
# it is not presented as independent proof of the server's identity.
if [[ ! -e "$marker" ]]; then
  printf '%s\n' "$expected_marker" | install -m 0644 /dev/stdin "$marker"
fi
docker version --format '{{.Server.Version}}'
docker compose version --short
systemctl is-active docker

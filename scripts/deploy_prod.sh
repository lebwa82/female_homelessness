#!/usr/bin/env bash
# Deploy a clean revision through a staged, deterministic gate.  This script is
# intentionally not a local-development helper: it runs only when an operator
# explicitly invokes `just deploy-prod` against the production host.
set -euo pipefail

host="${1:-}"
target_dir="${2:-/opt/women-help-bot}"
env_file="${WOMEN_HELP_ENV_FILE:-/etc/women-help-bot.env}"
postgres_container="${WOMEN_HELP_POSTGRES_CONTAINER:-women-help-bot_postgres_1}"

if [[ -z "$host" ]]; then
  host="$(uv run python -m scripts.resolve_prod_host)"
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to deploy: commit or stash local changes first." >&2
  exit 2
fi

revision="$(git rev-parse --short HEAD)"
archive_path="/tmp/women-help-${revision}.tar"

echo "Staging ${revision} for ${host}:${target_dir}"
git archive --format=tar HEAD |
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" "cat > '${archive_path}'"

ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" \
  "TARGET_DIR='${target_dir}' REVISION='${revision}' ARCHIVE_PATH='${archive_path}' ENV_FILE='${env_file}' POSTGRES_CONTAINER='${postgres_container}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

release_root="${TARGET_DIR}.releases"
staging_dir="${release_root}/.${REVISION}.staging.$$"
release_dir="${release_root}/${REVISION}"
next_link="${TARGET_DIR}.next"
previous_target=""

cleanup() {
  sudo rm -f "$ARCHIVE_PATH"
  sudo rm -rf "$staging_dir"
}
trap cleanup EXIT

run_staged() {
  # The root-only EnvironmentFile is sourced only inside this root shell.  Its
  # values are never echoed, exported by the deploy client, or written to logs.
  sudo bash -c '
    set -euo pipefail
    set -a
    . "$1"
    set +a
    cd "$2"
    shift 2
    exec "$@"
  ' -- "$ENV_FILE" "$staging_dir" "$@"
}

rollback() {
  if [[ -n "$previous_target" ]]; then
    sudo ln -sfn "$previous_target" "$next_link"
    sudo mv -Tf "$next_link" "$TARGET_DIR"
    sudo systemctl restart women-help-bot || true
  fi
}

if ! sudo test -r "$ENV_FILE"; then
  echo "Refusing activation: root-only service EnvironmentFile is unavailable." >&2
  exit 3
fi

sudo mkdir -p "$release_root"
sudo mkdir "$staging_dir"
sudo tar -xf "$ARCHIVE_PATH" -C "$staging_dir"

# All checks operate from the staged artifact before it can replace the active
# project.  db-assure uses the existing root-only database configuration.
run_staged uv sync --all-groups --locked
run_staged just check
run_staged just scenario-smoke
run_staged just eval-dialogues

# The target database must already be healthy.  Do not start, recreate, or
# otherwise mutate the container from deployment code.
if ! sudo podman inspect --format '{{.State.Health.Status}}' "$POSTGRES_CONTAINER" 2>/dev/null | grep -qx healthy; then
  echo "Refusing activation: production PostgreSQL is not healthy." >&2
  exit 4
fi
run_staged just db-assure

if [[ -e "$release_dir" ]]; then
  echo "Refusing activation: release revision already exists." >&2
  exit 5
fi
sudo mv "$staging_dir" "$release_dir"

# Convert the original project directory to a release symlink once; later
# activation is an atomic symlink replacement and can be rolled back.
if [[ -e "$TARGET_DIR" && ! -L "$TARGET_DIR" ]]; then
  legacy_dir="${release_root}/legacy-before-${REVISION}"
  sudo mv "$TARGET_DIR" "$legacy_dir"
  previous_target="$legacy_dir"
elif [[ -L "$TARGET_DIR" ]]; then
  previous_target="$(sudo readlink -f "$TARGET_DIR")"
fi

sudo ln -sfn "$release_dir" "$next_link"
sudo mv -Tf "$next_link" "$TARGET_DIR"
if ! sudo systemctl restart women-help-bot || ! sudo systemctl is-active --quiet women-help-bot; then
  echo "Activation failed; restoring the previous release." >&2
  rollback
  exit 6
fi

echo "Deployment ${REVISION} activated after staged checks and PostgreSQL assurance."
REMOTE_SCRIPT

echo "Deployment ${revision} completed."

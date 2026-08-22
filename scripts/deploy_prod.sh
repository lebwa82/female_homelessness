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
legacy_dir=""
legacy_moved=0
target_replaced=0
activation_started=0
db_assure_env=""
# The override exists solely for local shell stubs; operators do not need it
# and the default is the fixed root-owned command path used in production.
staged_path="${WOMEN_HELP_STAGED_PATH:-/usr/local/bin:/usr/bin:/bin}"

cleanup() {
  sudo rm -f "$ARCHIVE_PATH"
  sudo rm -rf "$staging_dir"
  if [[ -n "$db_assure_env" ]]; then
    sudo rm -f "$db_assure_env"
  fi
}
trap cleanup EXIT

run_staged() {
  # All non-database gates are isolated from root-only service credentials.
  # The synthetic endpoint is deliberately unreachable: checks must stay
  # offline and cannot accidentally target production PostgreSQL.
  sudo env -i \
    PATH="$staged_path" \
    DATABASE_URL=postgresql+asyncpg://offline \
    /bin/bash -c '
    cd "$1"
    shift
    exec "$@"
  ' -- "$staging_dir" "$@"
}

run_staged_db_assure() {
  # Read one strict data line from the root-owned temporary file.  This is not
  # `source`/`eval`: quote characters and shell syntax were rejected before
  # the file was created, and the value never appears in argv or logs.
  sudo env -i \
    PATH="$staged_path" \
    WOMEN_HELP_DB_ENV_FILE="$db_assure_env" \
    /bin/bash -c '
    set -euo pipefail
    IFS= read -r database_line < "$WOMEN_HELP_DB_ENV_FILE"
    case "$database_line" in DATABASE_URL=*) ;; *) exit 2;; esac
    DATABASE_URL="${database_line#DATABASE_URL=}"
    export DATABASE_URL
    cd "$1"
    exec just db-assure
  ' -- "$staging_dir"
}

rollback() {
  trap - ERR
  set +e
  if [[ "$legacy_moved" -eq 1 && -n "$legacy_dir" ]]; then
    # The first activation converted a real project directory.  Restore that
    # exact directory rather than leaving a symlink to its emergency location.
    sudo rm -f "$TARGET_DIR"
    sudo mv -Tf "$legacy_dir" "$TARGET_DIR"
  elif [[ "$target_replaced" -eq 1 && -n "$previous_target" ]]; then
    sudo ln -sfn "$previous_target" "$next_link"
    sudo mv -Tf "$next_link" "$TARGET_DIR"
  fi
  sudo systemctl restart women-help-bot || true
}

activation_error() {
  local status="$?"
  if [[ "$activation_started" -eq 1 ]]; then
    echo "Activation failed; restoring the previous release." >&2
    rollback
  fi
  exit "$status"
}
trap activation_error ERR

read_database_url() {
  # The strict grammar intentionally permits one unquoted, non-empty URL only.
  # It is a reversible operational ruling: production EnvironmentFile values
  # needing shell quotes must be percent-encoded before deployment.
  sudo awk '
    BEGIN { found = 0 }
    /^[[:space:]]*DATABASE_URL=/ {
      found += 1
      if (found != 1) exit 2
      value = $0
      sub(/^[[:space:]]*DATABASE_URL=/, "", value)
      if (value == "" || value ~ /[[:space:]"\\]/ || index(value, "$") || index(value, "`") || index(value, sprintf("%c", 39))) exit 2
      print "DATABASE_URL=" value
    }
    END { if (found != 1) exit 2 }
  ' "$ENV_FILE"
}

sudo mkdir -p "$release_root"
sudo mkdir "$staging_dir"
sudo tar -xf "$ARCHIVE_PATH" -C "$staging_dir"

# All checks operate from the staged artifact before it can replace the active
# project.  They receive a synthetic, deliberately unusable database URL.
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
if ! sudo test -r "$ENV_FILE"; then
  echo "Refusing activation: root-only service EnvironmentFile is unavailable." >&2
  exit 3
fi
db_assure_env="$(sudo mktemp /tmp/women-help-db-assure.XXXXXX)"
if ! read_database_url | sudo tee "$db_assure_env" >/dev/null; then
  echo "Refusing activation: service EnvironmentFile has no safe database value." >&2
  exit 3
fi
run_staged_db_assure

if [[ -e "$release_dir" ]]; then
  echo "Refusing activation: release revision already exists." >&2
  exit 5
fi
sudo mv "$staging_dir" "$release_dir"

# Convert the original project directory to a release symlink once; later
# activation is an atomic symlink replacement and can be rolled back.
activation_started=1
if [[ -e "$TARGET_DIR" && ! -L "$TARGET_DIR" ]]; then
  legacy_dir="${release_root}/legacy-before-${REVISION}"
  previous_target="$legacy_dir"
  sudo mv "$TARGET_DIR" "$legacy_dir"
  legacy_moved=1
elif [[ -L "$TARGET_DIR" ]]; then
  previous_target="$(sudo readlink -f "$TARGET_DIR")"
fi

sudo ln -sfn "$release_dir" "$next_link"
sudo mv -Tf "$next_link" "$TARGET_DIR"
target_replaced=1
sudo systemctl restart women-help-bot
sudo systemctl is-active --quiet women-help-bot
activation_started=0
trap - ERR

echo "Deployment ${REVISION} activated after staged checks and PostgreSQL assurance."
REMOTE_SCRIPT

echo "Deployment ${revision} completed."

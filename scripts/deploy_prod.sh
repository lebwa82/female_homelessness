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
readonly SUDO_BIN="/usr/bin/sudo"
readonly ENV_BIN="/usr/bin/env"
readonly BASH_BIN="/bin/bash"
readonly UV_BIN="/usr/local/bin/uv"
readonly JUST_BIN="/usr/local/bin/just"
readonly PODMAN_BIN="/usr/bin/podman"
readonly SYSTEMCTL_BIN="/usr/bin/systemctl"
readonly STAT_BIN="/usr/bin/stat"
readonly AWK_BIN="/usr/bin/awk"
readonly GREP_BIN="/usr/bin/grep"
readonly TEE_BIN="/usr/bin/tee"
readonly MKTEMP_BIN="/usr/bin/mktemp"
readonly TEST_BIN="/bin/test"
readonly READLINK_BIN="/usr/bin/readlink"
readonly RM_BIN="/bin/rm"
readonly MKDIR_BIN="/bin/mkdir"
readonly TAR_BIN="/usr/bin/tar"
readonly MV_BIN="/bin/mv"
readonly LN_BIN="/bin/ln"
readonly PRIVILEGED_PATH="/usr/local/bin:/usr/bin:/bin"
readonly VERIFY_ROOT_TOOLS=1

verify_root_tool() {
  local tool="$1" metadata owner mode
  [[ -e "$tool" ]] || {
    echo "Refusing deployment: fixed production tool is missing: $tool" >&2
    exit 6
  }
  metadata="$($STAT_BIN -c '%u:%a' -- "$tool")"
  owner="${metadata%%:*}"
  mode="${metadata#*:}"
  if [[ "$owner" != "0" ]] || (( (8#$mode & 0022) != 0 )); then
    echo "Refusing deployment: production tool is not root-owned and non-writable: $tool" >&2
    exit 6
  fi
}

if [[ "$VERIFY_ROOT_TOOLS" -eq 1 ]]; then
  for tool in \
    "$SUDO_BIN" "$ENV_BIN" "$BASH_BIN" "$UV_BIN" "$JUST_BIN" \
    "$PODMAN_BIN" "$SYSTEMCTL_BIN" "$STAT_BIN" "$AWK_BIN" "$GREP_BIN" \
    "$TEE_BIN" "$MKTEMP_BIN" "$TEST_BIN" "$READLINK_BIN" "$RM_BIN" \
    "$MKDIR_BIN" "$TAR_BIN" "$MV_BIN" "$LN_BIN" \
    / /usr /usr/local /usr/local/bin /usr/bin /bin
  do
    verify_root_tool "$tool"
  done
fi

cleanup() {
  "$SUDO_BIN" "$RM_BIN" -f "$ARCHIVE_PATH"
  "$SUDO_BIN" "$RM_BIN" -rf "$staging_dir"
  if [[ -n "$db_assure_env" ]]; then
    "$SUDO_BIN" "$RM_BIN" -f "$db_assure_env"
  fi
}
trap cleanup EXIT

run_staged() {
  # All non-database gates are isolated from root-only service credentials.
  # The synthetic endpoint is deliberately unreachable: checks must stay
  # offline and cannot accidentally target production PostgreSQL.
  "$SUDO_BIN" "$ENV_BIN" -i \
    PATH="$PRIVILEGED_PATH" \
    DATABASE_URL=postgresql+asyncpg://offline \
    "$BASH_BIN" -c '
    cd "$1"
    shift
    exec "$@"
  ' -- "$staging_dir" "$@"
}

run_staged_db_assure() {
  # Read one strict data line from the root-owned temporary file.  This is not
  # `source`/`eval`: quote characters and shell syntax were rejected before
  # the file was created, and the value never appears in argv or logs.
  "$SUDO_BIN" "$ENV_BIN" -i \
    PATH="$PRIVILEGED_PATH" \
    WOMEN_HELP_DB_ENV_FILE="$db_assure_env" \
    "$BASH_BIN" -c '
    set -euo pipefail
    IFS= read -r database_line < "$WOMEN_HELP_DB_ENV_FILE"
    case "$database_line" in DATABASE_URL=*) ;; *) exit 2;; esac
    DATABASE_URL="${database_line#DATABASE_URL=}"
    export DATABASE_URL
    cd "$1"
    exec "$2" db-assure
  ' -- "$staging_dir" "$JUST_BIN"
}

rollback() {
  trap - ERR
  set +e
  if [[ "$legacy_moved" -eq 1 && -n "$legacy_dir" ]]; then
    # The first activation converted a real project directory.  Restore that
    # exact directory rather than leaving a symlink to its emergency location.
    "$SUDO_BIN" "$RM_BIN" -f "$TARGET_DIR"
    "$SUDO_BIN" "$MV_BIN" -Tf "$legacy_dir" "$TARGET_DIR"
  elif [[ "$target_replaced" -eq 1 && -n "$previous_target" ]]; then
    "$SUDO_BIN" "$LN_BIN" -sfn "$previous_target" "$next_link"
    "$SUDO_BIN" "$MV_BIN" -Tf "$next_link" "$TARGET_DIR"
  fi
  "$SUDO_BIN" "$SYSTEMCTL_BIN" restart women-help-bot || true
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
  "$SUDO_BIN" "$AWK_BIN" '
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

"$SUDO_BIN" "$MKDIR_BIN" -p "$release_root"
"$SUDO_BIN" "$MKDIR_BIN" "$staging_dir"
"$SUDO_BIN" "$TAR_BIN" -xf "$ARCHIVE_PATH" -C "$staging_dir"

# All checks operate from the staged artifact before it can replace the active
# project.  They receive a synthetic, deliberately unusable database URL.
run_staged "$UV_BIN" sync --all-groups --locked
run_staged "$JUST_BIN" check
run_staged "$JUST_BIN" scenario-smoke
run_staged "$JUST_BIN" eval-dialogues

# The target database must already be healthy.  Do not start, recreate, or
# otherwise mutate the container from deployment code.
if ! "$SUDO_BIN" "$PODMAN_BIN" inspect --format '{{.State.Health.Status}}' "$POSTGRES_CONTAINER" 2>/dev/null | "$GREP_BIN" -qx healthy; then
  echo "Refusing activation: production PostgreSQL is not healthy." >&2
  exit 4
fi
if ! "$SUDO_BIN" "$TEST_BIN" -r "$ENV_FILE"; then
  echo "Refusing activation: root-only service EnvironmentFile is unavailable." >&2
  exit 3
fi
db_assure_env="$("$SUDO_BIN" "$MKTEMP_BIN" /tmp/women-help-db-assure.XXXXXX)"
if ! read_database_url | "$SUDO_BIN" "$TEE_BIN" "$db_assure_env" >/dev/null; then
  echo "Refusing activation: service EnvironmentFile has no safe database value." >&2
  exit 3
fi
run_staged_db_assure

if [[ -e "$release_dir" ]]; then
  echo "Refusing activation: release revision already exists." >&2
  exit 5
fi
"$SUDO_BIN" "$MV_BIN" "$staging_dir" "$release_dir"

# Convert the original project directory to a release symlink once; later
# activation is an atomic symlink replacement and can be rolled back.
activation_started=1
if [[ -e "$TARGET_DIR" && ! -L "$TARGET_DIR" ]]; then
  legacy_dir="${release_root}/legacy-before-${REVISION}"
  previous_target="$legacy_dir"
  "$SUDO_BIN" "$MV_BIN" "$TARGET_DIR" "$legacy_dir"
  legacy_moved=1
elif [[ -L "$TARGET_DIR" ]]; then
  previous_target="$("$SUDO_BIN" "$READLINK_BIN" -f "$TARGET_DIR")"
fi

"$SUDO_BIN" "$LN_BIN" -sfn "$release_dir" "$next_link"
"$SUDO_BIN" "$MV_BIN" -Tf "$next_link" "$TARGET_DIR"
target_replaced=1
"$SUDO_BIN" "$SYSTEMCTL_BIN" restart women-help-bot
"$SUDO_BIN" "$SYSTEMCTL_BIN" is-active --quiet women-help-bot
activation_started=0
trap - ERR

echo "Deployment ${REVISION} activated after staged checks and PostgreSQL assurance."
REMOTE_SCRIPT

echo "Deployment ${revision} completed."

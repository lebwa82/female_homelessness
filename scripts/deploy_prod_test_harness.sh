#!/usr/bin/env bash
# Non-privileged local harness for deploy_prod.sh. Production never reads the
# test variables; this wrapper rewrites a temporary copy with metadata adapters
# that preserve real writable bits while treating disposable fixture ownership
# as root-equivalent.
set -euo pipefail

tool_root="${WOMEN_HELP_TEST_TOOL_ROOT:?set WOMEN_HELP_TEST_TOOL_ROOT to the local stub directory}"
source_script="$(cd "$(dirname "$0")" && pwd)/deploy_prod.sh"
temporary_script="$(/usr/bin/mktemp /tmp/women-help-deploy-harness.XXXXXX)"
test_stat="$tool_root/.root-metadata-stat.$$"
test_readlink="$tool_root/.root-metadata-readlink.$$"
trap '/bin/rm -f "$temporary_script" "$test_stat" "$test_readlink"' EXIT

/bin/cat > "$test_stat" <<'TEST_STAT'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "-c" && "${2:-}" == "%F:%u:%a" && "${3:-}" == "--" && "$#" -eq 4 ]]
path="$4"
if [[ -n "${WOMEN_HELP_TEST_METADATA_LOG:-}" ]]; then
  printf '%s\n' "$path" >> "$WOMEN_HELP_TEST_METADATA_LOG"
fi
case "$path/" in
  "$WOMEN_HELP_TEST_TOOL_ROOT/"*)
    mode="$(/usr/bin/stat -f '%Lp' "$path" 2>/dev/null || true)"
    if [[ ! "$mode" =~ ^[0-7]+$ ]]; then
      mode="$(/usr/bin/stat -c '%a' -- "$path")"
    fi
    ;;
  *)
    # Fixed host paths are represented by their production-safe equivalent;
    # fixture paths retain real modes so writable-component tests are genuine.
    mode="755"
    ;;
esac
if [[ -L "$path" ]]; then
  kind="symbolic link"
elif [[ -d "$path" ]]; then
  kind="directory"
elif [[ -f "$path" ]]; then
  kind="regular file"
else
  kind="other"
fi
printf '%s:0:%s\n' "$kind" "$mode"
TEST_STAT

/bin/cat > "$test_readlink" <<'TEST_READLINK'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--" ]]; then
  shift
fi
exec /usr/bin/readlink "$@"
TEST_READLINK

/bin/chmod 0700 "$test_stat" "$test_readlink"

/usr/bin/sed \
  -e "s|readonly SUDO_BIN=\"/usr/bin/sudo\"|readonly SUDO_BIN=\"$tool_root/sudo\"|" \
  -e "s|readonly UV_BIN=\"/usr/local/bin/uv\"|readonly UV_BIN=\"$tool_root/uv\"|" \
  -e "s|readonly JUST_BIN=\"/usr/local/bin/just\"|readonly JUST_BIN=\"$tool_root/just\"|" \
  -e "s|readonly PODMAN_BIN=\"/usr/bin/podman\"|readonly PODMAN_BIN=\"$tool_root/podman\"|" \
  -e "s|readonly SYSTEMCTL_BIN=\"/usr/bin/systemctl\"|readonly SYSTEMCTL_BIN=\"$tool_root/systemctl\"|" \
  -e "s|readonly STAT_BIN=\"/usr/bin/stat\"|readonly STAT_BIN=\"$test_stat\"|" \
  -e "s|readonly READLINK_BIN=\"/usr/bin/readlink\"|readonly READLINK_BIN=\"$test_readlink\"|" \
  -e "s|readonly MV_BIN=\"/bin/mv\"|readonly MV_BIN=\"$tool_root/mv\"|" \
  "$source_script" > "$temporary_script"
/bin/chmod 0700 "$temporary_script"
/bin/bash "$temporary_script" "$@"

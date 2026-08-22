#!/usr/bin/env bash
# Non-privileged local harness for deploy_prod.sh.  Production never reads
# WOMEN_HELP_TEST_TOOL_ROOT; this wrapper rewrites a temporary copy only.
set -euo pipefail

tool_root="${WOMEN_HELP_TEST_TOOL_ROOT:?set WOMEN_HELP_TEST_TOOL_ROOT to the local stub directory}"
source_script="$(cd "$(dirname "$0")" && pwd)/deploy_prod.sh"
temporary_script="$(/usr/bin/mktemp /tmp/women-help-deploy-harness.XXXXXX)"
trap '/bin/rm -f "$temporary_script"' EXIT

/usr/bin/sed \
  -e "s|readonly SUDO_BIN=\"/usr/bin/sudo\"|readonly SUDO_BIN=\"$tool_root/sudo\"|" \
  -e "s|readonly UV_BIN=\"/usr/local/bin/uv\"|readonly UV_BIN=\"$tool_root/uv\"|" \
  -e "s|readonly JUST_BIN=\"/usr/local/bin/just\"|readonly JUST_BIN=\"$tool_root/just\"|" \
  -e "s|readonly PODMAN_BIN=\"/usr/bin/podman\"|readonly PODMAN_BIN=\"$tool_root/podman\"|" \
  -e "s|readonly SYSTEMCTL_BIN=\"/usr/bin/systemctl\"|readonly SYSTEMCTL_BIN=\"$tool_root/systemctl\"|" \
  -e "s|readonly MV_BIN=\"/bin/mv\"|readonly MV_BIN=\"$tool_root/mv\"|" \
  -e 's|readonly VERIFY_ROOT_TOOLS=1|readonly VERIFY_ROOT_TOOLS=0|' \
  "$source_script" > "$temporary_script"
/bin/chmod 0700 "$temporary_script"
/bin/bash "$temporary_script" "$@"

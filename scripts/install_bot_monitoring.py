"""Install an already staged, standalone watchdog release on the verified host.

Default --plan reads files only. Run --apply as root from the exact immutable
/opt/nevidimy/monitor/releases/<revision> directory after uploading a Git archive.
This installer never starts, stops or changes business services or their data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT_UID = 0
MARKER = "timeweb-nevidimy-prod-app-msk-1"
UNIT_NAMES = ("nevidimy-monitor.service", "nevidimy-monitor.timer")
CHECKS = {"telegram_poll", "telegram_proxy", "agent", "queues", "host"}
URL = re.compile(r"https://(?:incidents|uptime)\.betterstack\.com/api/v1/heartbeat/[A-Za-z0-9_-]+")


class InstallError(Exception):
    """A fixed safe error code; never include config content or command output."""


@dataclass(frozen=True)
class Layout:
    root: Path = Path("/")

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @property
    def monitor(self) -> Path:
        return self.path("/opt/nevidimy/monitor")

    @property
    def config(self) -> Path:
        return self.path("/etc/nevidimy-monitor.env")

    @property
    def state(self) -> Path:
        return self.path("/var/lib/nevidimy-monitor")

    def unit(self, name: str) -> Path:
        return self.path("/etc/systemd/system") / name


def regular(path: Path, *, private: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise InstallError("required_file_unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InstallError("regular_file_required")
    if private and stat.S_IMODE(info.st_mode) != 0o600:
        raise InstallError("private_config_required")


def external_configured(path: Path) -> bool:
    if not path.exists():
        return False
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise InstallError("config_format_invalid")
        values[key] = value
    try:
        urls = json.loads(values.get("BOT_MONITOR_HEARTBEAT_URLS_JSON", "{}"))
    except (TypeError, ValueError):
        raise InstallError("config_format_invalid") from None
    if not isinstance(urls, dict):
        raise InstallError("config_heartbeat_mapping_invalid")
    if not urls:
        return False
    if (
        not isinstance(urls, dict)
        or set(urls) != CHECKS
        or any(not isinstance(value, str) or not URL.fullmatch(value) for value in urls.values())
        or len(set(urls.values())) != len(CHECKS)
    ):
        raise InstallError("config_heartbeat_mapping_invalid")
    return True


def validate(revision: str, source: Path, layout: Layout, apply: bool) -> tuple[dict, str | None]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise InstallError("revision_invalid")
    expected = layout.monitor / "releases" / revision
    if source.absolute() != expected or source.resolve() != expected or not source.is_dir():
        raise InstallError("release_path_mismatch")
    if apply and os.geteuid() != 0:
        raise InstallError("root_required")
    marker = layout.path("/etc/nevidimy-deployment-target")
    regular(marker)
    if marker.read_text() not in {MARKER, MARKER + "\n"}:
        raise InstallError("deployment_target_mismatch")
    for relative in (
        "scripts/bot_monitor.py",
        "scripts/bot_monitor_probe.py",
        "scripts/chatwoot_compose.py",
        "deploy/timeweb/compose.monitor.yml",
        "app/monitoring/state.py",
        "app/monitoring/heartbeat.py",
        "deploy/timeweb/monitor.env.example",
        *("deploy/timeweb/" + name for name in UNIT_NAMES),
    ):
        candidate = source / relative
        regular(candidate)
        if candidate.resolve() != candidate:
            raise InstallError("release_symlink_forbidden")
    current = layout.monitor / "current"
    previous = None
    if os.path.lexists(current):
        if not current.is_symlink():
            raise InstallError("current_must_be_symlink")
        previous = os.readlink(current)
        target = (current.parent / previous).resolve()
        if target.parent != expected.parent or not re.fullmatch(r"[0-9a-f]{40}", target.name):
            raise InstallError("current_target_outside_monitor_releases")
    for directory in (layout.monitor, layout.state, layout.path("/etc/systemd/system")):
        if os.path.lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
            raise InstallError("owned_directory_invalid")
    changes = {}
    for name in UNIT_NAMES:
        destination = layout.unit(name)
        template = source / "deploy/timeweb" / name
        if os.path.lexists(destination):
            regular(destination)
            changes[name] = (
                destination.read_bytes() != template.read_bytes()
                or stat.S_IMODE(destination.stat().st_mode) != 0o644
                or destination.stat().st_uid != ROOT_UID
            )
        else:
            changes[name] = True
    config_exists = os.path.lexists(layout.config)
    if config_exists:
        regular(layout.config, private=True)
    configured = external_configured(layout.config) if config_exists else False
    changes["current"] = previous != str(source)
    changes["config"] = not config_exists
    changes["state_directory"] = (
        not layout.state.exists()
        or stat.S_IMODE(layout.state.stat().st_mode) != 0o700
        or layout.state.stat().st_uid != ROOT_UID
    )
    return {"changes": changes, "external_configured": configured}, previous


def atomic_file(path: Path, content: bytes, mode: int, *, replace: bool = True) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".nevidimy-monitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), mode)
            os.fchown(target.fileno(), 0, 0)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def backup(layout: Layout, previous: str | None) -> Path:
    backups = layout.monitor / "backups"
    if os.path.lexists(backups) and (backups.is_symlink() or not backups.is_dir()):
        raise InstallError("backup_directory_invalid")
    backups.mkdir(mode=0o700, exist_ok=True)
    backups.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backups / stamp
    destination.mkdir(mode=0o700)
    for name in UNIT_NAMES:
        path = layout.unit(name)
        if path.exists():
            atomic_file(destination / name, path.read_bytes(), 0o600)
    if layout.config.exists():
        atomic_file(destination / "nevidimy-monitor.env", layout.config.read_bytes(), 0o600)
    atomic_file(destination / "current-target.json", json.dumps(previous).encode(), 0o600)
    return destination


def systemctl(arguments: list[str]) -> None:
    try:
        result = subprocess.run(
            ["systemctl", *arguments], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("systemctl_unavailable") from None
    if result.returncode:
        raise InstallError("systemctl_failed")


def install(
    revision: str,
    source: Path,
    *,
    apply: bool = False,
    layout: Layout | None = None,
    run_systemctl=systemctl,
) -> dict:
    layout = layout or Layout()
    plan, previous = validate(revision, source, layout, apply)
    changes = plan["changes"]
    commands = []
    if any(changes[name] for name in UNIT_NAMES):
        commands.append(["daemon-reload"])
    commands.append(["enable", "--now", "nevidimy-monitor.timer"])
    result = {
        "mode": "apply" if apply else "plan",
        "revision": revision,
        **plan,
        "systemctl": commands,
        "backup_created": False,
    }
    if not apply:
        return result
    # Backup is bounded to this watchdog's units, link target and configuration.
    if any(changes.values()):
        backup(layout, previous)
        result["backup_created"] = True
    for name in UNIT_NAMES:
        if changes[name]:
            destination = layout.unit(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_file(destination, (source / "deploy/timeweb" / name).read_bytes(), 0o644)
    if changes["config"]:
        atomic_file(
            layout.config,
            (source / "deploy/timeweb/monitor.env.example").read_bytes(),
            0o600,
            replace=False,
        )
    if changes["state_directory"]:
        layout.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        layout.state.chmod(0o700)
        os.chown(layout.state, 0, 0)
    if changes["current"]:
        # mkstemp reserves an owned temporary name; only that file is removed.
        fd, temporary = tempfile.mkstemp(prefix=".current-", dir=layout.monitor)
        os.close(fd)
        os.unlink(temporary)
        try:
            os.symlink(str(source), temporary)
            os.replace(temporary, layout.monitor / "current")
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    for command in commands:
        run_systemctl(command)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="read-only, default")
    mode.add_argument("--apply", action="store_true", help="install watchdog only")
    parser.add_argument("--revision", required=True, help="40 lowercase hexadecimal Git revision")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = install(args.revision, args.source, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - never echo config/command output
        result = {"error_type": type(error).__name__}
        if isinstance(error, InstallError):
            result["error_code"] = str(error)
        print(json.dumps(result), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

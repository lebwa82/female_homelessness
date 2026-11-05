"""Filesystem fixture tests. Never run real systemctl or modify the host."""

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import install_bot_monitoring as setup

REVISION = "a" * 40
OLD_REVISION = "b" * 40


@pytest.fixture
def release(tmp_path, monkeypatch):
    layout = setup.Layout(tmp_path)
    source = layout.monitor / "releases" / REVISION
    files = {
        "scripts/bot_monitor.py": "# monitor\n",
        "scripts/bot_monitor_probe.py": "# probe\n",
        "scripts/chatwoot_compose.py": "# compose\n",
        "deploy/timeweb/compose.monitor.yml": "services: {}\n",
        "app/monitoring/state.py": "# state\n",
        "app/monitoring/heartbeat.py": "# heartbeat\n",
        "deploy/timeweb/nevidimy-monitor.service": "[Service]\nExecStart=/usr/bin/python3 -B /opt/nevidimy/monitor/current/scripts/bot_monitor.py\n",
        "deploy/timeweb/nevidimy-monitor.timer": "[Timer]\nOnUnitActiveSec=60\n",
        "deploy/timeweb/monitor.env.example": "BOT_MONITOR_HEARTBEAT_URLS_JSON={}\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    marker = layout.path("/etc/nevidimy-deployment-target")
    marker.parent.mkdir(parents=True)
    marker.write_text(setup.MARKER + "\n")
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup, "ROOT_UID", os.getuid())
    monkeypatch.setattr(setup.os, "fchown", lambda *args: None)
    monkeypatch.setattr(setup.os, "chown", lambda *args: None)
    return source, layout


def snapshot(root):
    return {
        str(path.relative_to(root)): (
            ("link", os.readlink(path))
            if path.is_symlink()
            else ("file", path.read_bytes(), path.stat().st_mode & 0o777)
            if path.is_file()
            else ("dir", path.stat().st_mode & 0o777)
        )
        for path in root.rglob("*")
    }


def test_default_plan_is_read_only(release):
    source, layout = release
    before = snapshot(layout.root)
    calls = []
    result = setup.install(REVISION, source, layout=layout, run_systemctl=calls.append)
    assert result["mode"] == "plan" and result["external_configured"] is False
    assert result["changes"]["config"] is True
    assert snapshot(layout.root) == before and calls == []


@pytest.mark.parametrize(
    "problem,code",
    [
        ("marker", "deployment_target_mismatch"),
        ("revision", "revision_invalid"),
        ("path", "release_path_mismatch"),
        ("root", "root_required"),
        ("source_symlink", "release_path_mismatch"),
    ],
)
def test_validation_fails_before_writes(release, monkeypatch, problem, code):
    source, layout = release
    revision = REVISION
    if problem == "marker":
        layout.path("/etc/nevidimy-deployment-target").write_text("other-host\n")
    elif problem == "revision":
        revision = "a" * 39
    elif problem == "path":
        source = source.parent
    elif problem == "root":
        monkeypatch.setattr(setup.os, "geteuid", lambda: 502)
    else:
        alias = source.parent / "alias"
        alias.symlink_to(source)
        source = alias
    before = snapshot(layout.root)
    calls = []
    with pytest.raises(setup.InstallError, match=code):
        setup.install(revision, source, apply=True, layout=layout, run_systemctl=calls.append)
    assert snapshot(layout.root) == before and calls == []


def test_apply_installs_only_monitor_and_is_idempotent(release):
    source, layout = release
    calls = []
    first = setup.install(REVISION, source, apply=True, layout=layout, run_systemctl=calls.append)
    assert first["backup_created"] is True
    assert calls == [["daemon-reload"], ["enable", "--now", "nevidimy-monitor.timer"]]
    assert (layout.monitor / "current").resolve() == source
    assert layout.state.stat().st_mode & 0o777 == 0o700
    assert layout.config.stat().st_mode & 0o777 == 0o600
    assert setup.external_configured(layout.config) is False
    for name in setup.UNIT_NAMES:
        assert layout.unit(name).stat().st_mode & 0o777 == 0o644
    backups = list((layout.monitor / "backups").iterdir())
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o700
    before = snapshot(layout.root)
    calls.clear()
    second = setup.install(REVISION, source, apply=True, layout=layout, run_systemctl=calls.append)
    assert second["backup_created"] is False
    assert not any(second["changes"].values())
    assert snapshot(layout.root) == before
    assert calls == [["enable", "--now", "nevidimy-monitor.timer"]]


def test_preserves_config_and_state_and_backs_up_prior_monitor_files(release):
    source, layout = release
    urls = {
        check: "https://incidents.betterstack.com/api/v1/heartbeat/secret" + check
        for check in setup.CHECKS
    }
    config = "BOT_MONITOR_HEARTBEAT_URLS_JSON=" + json.dumps(urls) + "\nEXTRA=secret\n"
    layout.config.write_text(config)
    layout.config.chmod(0o600)
    layout.state.mkdir(parents=True, mode=0o700)
    state = layout.state / "state.json"
    state.write_text('{"preserved": true}')
    old_target = layout.monitor / "releases" / OLD_REVISION
    old_target.mkdir()
    (layout.monitor / "current").symlink_to(old_target)
    for name in setup.UNIT_NAMES:
        layout.unit(name).parent.mkdir(parents=True, exist_ok=True)
        layout.unit(name).write_text("previous " + name)
    result = setup.install(
        REVISION, source, apply=True, layout=layout, run_systemctl=lambda _: None
    )
    assert result["external_configured"] is True
    assert layout.config.read_text() == config
    assert state.read_text() == '{"preserved": true}'
    backup = next((layout.monitor / "backups").iterdir())
    assert (backup / "nevidimy-monitor.env").read_text() == config
    assert json.loads((backup / "current-target.json").read_text()) == str(old_target)
    assert {path.name for path in backup.iterdir()} == {
        *setup.UNIT_NAMES,
        "nevidimy-monitor.env",
        "current-target.json",
    }
    for name in setup.UNIT_NAMES:
        assert (backup / name).read_text() == "previous " + name
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "problem",
    [
        "current_directory",
        "external_current",
        "config_symlink",
        "state_symlink",
        "unit_symlink",
        "config_permissions",
    ],
)
def test_refuses_unsafe_destinations_without_changes(release, problem):
    source, layout = release
    if problem == "current_directory":
        (layout.monitor / "current").mkdir()
    elif problem == "external_current":
        (layout.monitor / "current").symlink_to(layout.path("/opt/nevidimy/app"))
    elif problem == "config_symlink":
        layout.config.symlink_to(source / "deploy/timeweb/monitor.env.example")
    elif problem == "state_symlink":
        layout.state.parent.mkdir(parents=True)
        layout.state.symlink_to(source)
    elif problem == "unit_symlink":
        layout.unit(setup.UNIT_NAMES[0]).parent.mkdir(parents=True)
        layout.unit(setup.UNIT_NAMES[0]).symlink_to(source / "scripts/bot_monitor.py")
    else:
        layout.config.write_text("BOT_MONITOR_HEARTBEAT_URLS_JSON={}\n")
        layout.config.chmod(0o644)
    before = snapshot(layout.root)
    with pytest.raises(setup.InstallError):
        setup.install(REVISION, source, apply=True, layout=layout, run_systemctl=lambda _: None)
    assert snapshot(layout.root) == before


def test_systemctl_is_bounded_and_hides_command_output(monkeypatch):
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=1, stdout="secret", stderr="secret")

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    with pytest.raises(setup.InstallError, match="^systemctl_failed$"):
        setup.systemctl(["daemon-reload"])
    arguments, options = calls[0]
    assert arguments == ["systemctl", "daemon-reload"]
    assert options["timeout"] == 30 and options["capture_output"] is True


def test_failed_enable_leaves_recoverable_backup_and_preserves_state(release):
    source, layout = release

    def fail(_):
        raise setup.InstallError("systemctl_failed")

    with pytest.raises(setup.InstallError, match="systemctl_failed"):
        setup.install(REVISION, source, apply=True, layout=layout, run_systemctl=fail)
    assert len(list((layout.monitor / "backups").iterdir())) == 1
    assert layout.config.exists()
    assert layout.state.exists()
    assert (layout.monitor / "current").resolve() == source


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/install_bot_monitoring.py",
        "scripts/bot_monitor.py",
        "app/monitoring/state.py",
        "app/monitoring/heartbeat.py",
        "scripts/chatwoot_compose.py",
    ],
)
def test_host_runtime_modules_parse_with_python_310(relative):
    path = Path(__file__).resolve().parents[1] / relative
    ast.parse(path.read_text(), filename=relative, feature_version=(3, 10))


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/chatwoot_compose.py",
        "deploy/timeweb/compose.monitor.yml",
        "app/monitoring/heartbeat.py",
    ],
)
def test_missing_runtime_dependency_stops_before_writes(release, relative):
    source, layout = release
    (source / relative).unlink()
    before = snapshot(layout.root)
    calls = []
    with pytest.raises(setup.InstallError, match="required_file_unavailable"):
        setup.install(REVISION, source, apply=True, layout=layout, run_systemctl=calls.append)
    assert snapshot(layout.root) == before and calls == []

import configparser
import shlex
from pathlib import Path

import pytest

from scripts import chatwoot_compose

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/timeweb/nevidimy-certificate-admin.service"


def read_unit() -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None, strict=False)
    unit.read(UNIT, encoding="utf-8")
    return unit


def test_admin_unit_requires_core_and_checks_the_existing_target_and_environment():
    unit = read_unit()
    assert set(unit["Unit"]["Requires"].split()) == {"docker.service", "nevidimy.service"}
    assert set(unit["Unit"]["After"].split()) == {
        "docker.service",
        "nevidimy.service",
        "nevidimy-telegram.service",
        "nevidimy-proxy-tunnel.service",
    }
    assert unit["Service"]["WorkingDirectory"] == "/opt/nevidimy/app"
    assert unit["Service"]["Type"] == "oneshot"
    assert unit["Service"]["RemainAfterExit"] == "yes"
    assert unit["Install"]["WantedBy"] == "multi-user.target"
    checks = [line for line in UNIT.read_text().splitlines() if line.startswith("ExecStartPre=")]
    assert checks == [
        (
            "ExecStartPre=/usr/bin/grep -Fxq timeweb-nevidimy-prod-app-msk-1 "
            "/etc/nevidimy-deployment-target"
        ),
        "ExecStartPre=/usr/bin/test -s /etc/women-help-agent.env",
    ]


def test_admin_unit_only_starts_and_stops_the_prepared_admin_container():
    service = read_unit()["Service"]
    entrypoint = [
        "/usr/bin/python3",
        "scripts/chatwoot_compose.py",
        "--profile",
        "certificate-admin",
    ]
    assert shlex.split(service["ExecStart"]) == [
        *entrypoint,
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "--no-deps",
        "--wait",
        "--wait-timeout",
        "180",
        "certificate-admin-bot",
    ]
    assert shlex.split(service["ExecStop"]) == [*entrypoint, "stop", "certificate-admin-bot"]
    assert int(service["TimeoutStartSec"]) > 180
    assert int(service["TimeoutStopSec"]) > 0


@pytest.mark.parametrize("command", ["ExecStart", "ExecStop"])
def test_admin_commands_keep_the_active_compose_overlays(tmp_path, monkeypatch, command):
    environment = tmp_path / "platform.env"
    environment.write_text("CHATWOOT_INTERNAL_TLS=0\n")
    old_local_dropin = tmp_path / "http-local.conf"
    old_local_dropin.touch()
    public_dropin = tmp_path / "public-domain-https.conf"
    public_dropin.touch()
    monkeypatch.setattr(chatwoot_compose, "ENV_FILE", environment)
    monkeypatch.setattr(chatwoot_compose, "HTTP_LOCAL_DROPIN", old_local_dropin)
    monkeypatch.setattr(chatwoot_compose, "DOMAIN_HTTPS_DROPIN", public_dropin)
    arguments = shlex.split(read_unit()["Service"][command])[1:]
    monkeypatch.setattr(chatwoot_compose.sys, "argv", arguments)
    calls = []
    monkeypatch.setattr(
        chatwoot_compose.subprocess,
        "call",
        lambda args, **kwargs: calls.append((args, kwargs)) or 0,
    )

    assert chatwoot_compose.main() == 0
    invocation, options = calls[0]
    files = [invocation[index + 1] for index, value in enumerate(invocation) if value == "-f"]
    assert files == [
        "deploy/chatwoot/compose.yml",
        "deploy/chatwoot/compose.managed.yml",
        "deploy/timeweb/compose.yml",
        "deploy/timeweb/compose.domain-https.yml",
    ]
    assert invocation[-len(arguments[1:]) :] == arguments[1:]
    assert options["cwd"] == ROOT

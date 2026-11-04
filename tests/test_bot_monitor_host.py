"""Host orchestration fixtures: no Docker, systemd, credentials, or live HTTP."""

import copy
import json
import subprocess
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
import yaml

from app.monitoring.state import CHECKS
from scripts import bot_monitor as host
from scripts import chatwoot_compose as compose


def config():
    return {
        "urls": {
            check: f"https://incidents.betterstack.com/api/v1/heartbeat/synthetic_{check}"
            for check in CHECKS
        },
        "channel": "-1001234567890",
        "dashboard": "https://uptime.betterstack.com",
    }


def write_config(path, urls=None, **values):
    values = {
        "BOT_MONITOR_HEARTBEAT_URLS_JSON": json.dumps(config()["urls"] if urls is None else urls),
        **values,
    }
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    path.chmod(0o600)
    return path


def healthy():
    return {check: {"ok": True, "reason": "ok"} for check in CHECKS[:-1]}


def fake_items(tmp_path):
    ca = tmp_path / "ca.crt"
    bundle = tmp_path / "ca-bundle.pem"
    ca.write_text("synthetic-root-ca")
    bundle.write_text("synthetic-root-bundle")
    base = {
        "Image": "sha256:" + "a" * 64,
        "Config": {"Env": []},
        "NetworkSettings": {"Networks": {"nevidimy-timeweb_default": {}}},
        "Mounts": [
            {"Source": str(ca), "Destination": "/run/tls/ca.crt"},
            {"Source": str(bundle), "Destination": "/run/tls/ca-bundle.pem"},
        ],
        "State": {"Status": "running"},
    }
    items = {
        name: copy.deepcopy(base)
        for name in (
            "agent-bot",
            "telegram-ingress",
            "certificate-admin-bot",
        )
    }
    items["agent-bot"]["Config"]["Env"] = ["CHATWOOT_ACCOUNT_ID=1"]
    items["telegram-ingress"]["Config"]["Env"] = [
        "TELEGRAM_INGRESS_REDIS_URL=rediss://:synthetic-password@redis:6379/0",
        "TELEGRAM_BOT_TOKEN=12345678:synthetic-token",
    ]
    items["certificate-admin-bot"]["Config"]["Env"] = [
        "CERTIFICATE_ADMIN_BOT_TOKEN=23456789:synthetic-alert-token",
    ]
    return items


@pytest.fixture
def runtime(monkeypatch):
    now = [1000]
    answer = {"observations": healthy(), "telegram_delivered": []}
    requests = []
    heartbeats = Mock(return_value=True)
    monkeypatch.setattr(host.time, "time", lambda: now[0])
    monkeypatch.setattr(host, "containers", lambda: {"agent-bot": {"State": {"Status": "running"}}})
    monkeypatch.setattr(
        host, "command", Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    )
    monkeypatch.setattr(host, "heartbeat", heartbeats)

    def preparation(items, pending, settings):
        requests.append(copy.deepcopy(pending))
        return {"telegram_events": list(pending.values())}, {}

    monkeypatch.setattr(host, "probe_configuration", preparation)
    monkeypatch.setattr(host, "run_probe", lambda data, env: copy.deepcopy(answer))
    return now, answer, requests, heartbeats


def test_secure_configuration_and_missing_external_setup(tmp_path):
    missing = host.read_config(tmp_path / "absent.env")
    assert missing["urls"] == {}
    assert host.read_config(write_config(tmp_path / "valid.env"))["urls"] == config()["urls"]


@pytest.mark.parametrize(
    "bad",
    [
        "http://incidents.betterstack.com/api/v1/heartbeat/token",
        "https://incidents.betterstack.com.evil.example/api/v1/heartbeat/token",
        "https://incidents.betterstack.com@evil.example/api/v1/heartbeat/token",
        "https://incidents.betterstack.com/api/v1/heartbeat/token?forward=secret",
        "https://example.com/api/v1/heartbeat/token",
    ],
)
def test_heartbeat_config_rejects_nonofficial_or_ambiguous_destinations(tmp_path, bad):
    urls = config()["urls"]
    urls["agent"] = bad
    with pytest.raises(ValueError, match="heartbeat_destination_invalid"):
        host.read_config(write_config(tmp_path / "bad.env", urls))


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_exactly_five_unique_heartbeat_urls_required_when_configured(tmp_path, mutation):
    urls = config()["urls"]
    if mutation == "missing":
        urls.pop("host")
    elif mutation == "extra":
        urls["extra"] = "https://uptime.betterstack.com/api/v1/heartbeat/extra"
    else:
        urls["agent"] = urls["host"]
    with pytest.raises(ValueError):
        host.read_config(write_config(tmp_path / "bad.env", urls))


def test_config_permissions_symlinks_and_duplicate_keys_are_rejected(tmp_path):
    path = write_config(tmp_path / "settings.env")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="configuration_permissions"):
        host.read_config(path)
    path.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="configuration_permissions"):
        host.read_config(link)
    path.write_text("BOT_MONITOR_TELEGRAM_CHAT_ID=-1001234567\n" * 2)
    with pytest.raises(ValueError, match="configuration_format"):
        host.read_config(path)


def test_state_atomic_private_write_and_pending_roundtrip(tmp_path):
    path = tmp_path / "private" / "state.json"
    state = {
        "version": 1,
        "machine": {},
        "pending": {
            "agent": {"delivery_id": "synthetic-id", "text": "Сбой: Агент"},
        },
    }
    host.save_state(path, state)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert host.load_state(path) == state
    assert list(path.parent.iterdir()) == [path]


def test_failed_atomic_replacement_preserves_previous_state_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = {"version": 1, "machine": {}, "pending": {}}
    host.save_state(path, original)
    monkeypatch.setattr(host.os, "replace", Mock(side_effect=OSError("synthetic disk failure")))
    with pytest.raises(OSError):
        host.save_state(
            path, {**original, "pending": {"agent": {"delivery_id": "next", "text": "next"}}}
        )
    assert json.loads(path.read_text()) == original
    assert list(tmp_path.iterdir()) == [path]


def test_failed_state_serialization_preserves_previous_state_and_cleans_temp(tmp_path):
    path = tmp_path / "state.json"
    original = {"version": 1, "machine": {}, "pending": {}}
    host.save_state(path, original)
    with pytest.raises(ValueError):
        host.save_state(path, {"invalid": float("nan")})
    assert json.loads(path.read_text()) == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("damage", ["permissions", "symlink", "machine", "pending"])
def test_loading_insecure_or_corrupt_state_fails_closed(tmp_path, damage):
    path = tmp_path / "state.json"
    state = {"version": 1, "machine": {}, "pending": {}}
    if damage == "machine":
        state["machine"] = {"version": 1}
    elif damage == "pending":
        state["pending"] = {"unknown_check": {"delivery_id": "one", "text": "bounded"}}
    host.save_state(path, state)
    if damage == "permissions":
        path.chmod(0o644)
    elif damage == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    with pytest.raises(ValueError):
        host.load_state(path)


def test_probe_configuration_uses_inspected_image_network_and_existing_root_ca(tmp_path):
    items = fake_items(tmp_path)
    pending = {"agent": {"delivery_id": "delivery", "text": "Сбой: Агент"}}
    data, env = host.probe_configuration(items, pending, config())
    assert env["MONITOR_IMAGE"] == items["agent-bot"]["Image"]
    assert env["MONITOR_NETWORK"] == "nevidimy-timeweb_default"
    assert env["MONITOR_CA_SOURCE"] == str(tmp_path / "ca.crt")
    assert env["MONITOR_CA_BUNDLE_SOURCE"] == str(tmp_path / "ca-bundle.pem")
    assert data["ca_file"] == "/run/tls/ca-bundle.pem"
    assert data["proxy_url"] == "http://telegram-proxy:3128"
    assert data["agent_health_url"] == "https://agent-bot:8080/healthz"
    assert data["telegram_events"] == [{"event_id": "delivery", "text": "Сбой: Агент"}]


@pytest.mark.parametrize("damage", ["tagged_image", "multiple_networks", "missing_ca"])
def test_unsafe_or_ambiguous_probe_assembly_is_rejected(tmp_path, damage):
    items = fake_items(tmp_path)
    if damage == "tagged_image":
        items["agent-bot"]["Image"] = "nevidimy:latest"
    elif damage == "multiple_networks":
        for name in ("agent-bot", "telegram-ingress"):
            items[name]["NetworkSettings"]["Networks"]["other"] = {}
    else:
        (tmp_path / "ca.crt").unlink()
    with pytest.raises(ValueError):
        host.probe_configuration(items, {}, config())


def test_monitor_manifest_is_non_disruptive_and_has_no_docker_socket():
    manifest = yaml.safe_load((host.ROOT / "deploy/timeweb/compose.monitor.yml").read_text())
    assert set(manifest["services"]) == {"probe"}
    probe = manifest["services"]["probe"]
    assert probe["image"].startswith("${MONITOR_IMAGE:?")
    assert probe["pull_policy"] == "never"
    assert probe["read_only"] is True
    assert probe["user"] == "65534:65534"
    assert probe["cap_drop"] == ["ALL"]
    assert probe["networks"] == ["runtime"]
    assert manifest["networks"]["runtime"]["external"] is True
    assert manifest["networks"]["runtime"]["name"].startswith("${MONITOR_NETWORK:?")
    assert {mount["target"] for mount in probe["volumes"]} == {
        "/monitor/probe.py",
        "/run/tls/ca.crt",
        "/run/tls/ca-bundle.pem",
    }
    assert all(
        mount["read_only"] and not mount["bind"]["create_host_path"] for mount in probe["volumes"]
    )
    assert "docker.sock" not in json.dumps(manifest)
    assert "ports" not in probe and "depends_on" not in probe


def test_monitor_compose_helper_fixes_project_manifest_and_ignores_compose_environment(monkeypatch):
    caller = Mock(return_value=0)
    monkeypatch.setattr(compose.subprocess, "call", caller)
    monkeypatch.setenv("COMPOSE_FILE", "malicious.yml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "existing-bot")
    monkeypatch.setattr(
        compose.sys, "argv", ["compose", "--monitoring", "run", "--rm", "--no-deps", "probe"]
    )
    assert compose.main() == 0
    args, kwargs = caller.call_args
    assert args[0][:7] == [
        "docker",
        "compose",
        "--project-name",
        "nevidimy-monitor",
        "--env-file",
        "/dev/null",
        "-f",
    ]
    assert args[0][7] == str(host.ROOT / "deploy/timeweb/compose.monitor.yml")
    assert not any(key.startswith("COMPOSE_") for key in kwargs["env"])


@pytest.mark.parametrize(
    "args", [["up", "-d"], ["run", "-f", "other.yml"], ["run", "--project-name=core", "probe"]]
)
def test_monitor_helper_refuses_core_service_mutations_and_manifest_overrides(monkeypatch, args):
    caller = Mock()
    monkeypatch.setattr(compose.subprocess, "call", caller)
    monkeypatch.setattr(compose.sys, "argv", ["compose", "--monitoring", *args])
    assert compose.main() == 2
    caller.assert_not_called()


def test_run_probe_is_bounded_oneoff_without_dependencies(monkeypatch):
    command = Mock(
        return_value=subprocess.CompletedProcess([], 0, json.dumps({"observations": healthy()}), "")
    )
    monkeypatch.setattr(host, "command", command)
    host.run_probe({"synthetic": "input"}, {"MONITOR_IMAGE": "sha256:synthetic"})
    args, kwargs = command.call_args
    assert args[0][2:] == [
        "--monitoring",
        "run",
        "--rm",
        "--no-deps",
        "--name",
        host.PROBE_NAME,
        "-T",
        "probe",
    ]
    assert kwargs["timeout"] == 27
    assert json.loads(kwargs["input"]) == {"synthetic": "input"}


def test_probe_timeout_removes_only_named_monitor_container(monkeypatch):
    command = Mock(
        side_effect=[subprocess.TimeoutExpired("synthetic", 27), subprocess.CompletedProcess([], 0)]
    )
    monkeypatch.setattr(host, "command", command)
    with pytest.raises(ValueError, match="probe_timeout"):
        host.run_probe({}, {})
    assert command.call_args_list[1].args[0] == ["docker", "rm", "-f", host.PROBE_NAME]


@pytest.mark.parametrize("ok", [True, False])
def test_heartbeat_disables_environment_proxies_and_redirects(monkeypatch, ok):
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-untrusted-proxy:9999")
    response = Mock(status=204)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    factory = Mock(return_value=opener)
    monkeypatch.setattr(host, "build_opener", factory)
    url = config()["urls"]["agent"]
    assert host.heartbeat(url, ok) is True
    assert factory.call_args.args[0].proxies == {}
    blocker = opener.add_handler.call_args.args[0]
    assert blocker.redirect_request(None, None, 302, "redirect", {}, "https://evil.example") is None
    request = opener.open.call_args.args[0]
    assert request.full_url == url + ("" if ok else "/fail")
    assert request.get_method() == "POST"
    assert request.data == b""
    assert opener.open.call_args.kwargs["timeout"] == 3


def test_redirect_failure_is_not_retried_or_logged(monkeypatch, capsys):
    opener = Mock()
    url = config()["urls"]["agent"]
    opener.open.side_effect = HTTPError(url, 302, "synthetic secret", {}, None)
    monkeypatch.setattr(host, "build_opener", Mock(return_value=opener))
    assert host.heartbeat(url, True) is False
    assert opener.open.call_count == 1
    assert capsys.readouterr() == ("", "")


def test_dry_run_sends_no_heartbeat_or_telegram_and_does_not_persist(tmp_path, runtime):
    _, _, requests, heartbeats = runtime
    path = tmp_path / "state.json"
    state = {
        "version": 1,
        "machine": {},
        "pending": {
            "agent": {"delivery_id": "old", "text": "Сбой: Агент"},
        },
    }
    host.save_state(path, state)
    before = path.read_bytes()
    host.tick(config(), path, dry_run=True)
    assert path.read_bytes() == before
    assert requests == [{}]
    heartbeats.assert_not_called()


def test_missing_external_configuration_runs_local_checks_without_heartbeats(tmp_path, runtime):
    _, _, _, heartbeats = runtime
    result = host.tick({**config(), "urls": {}}, tmp_path / "state.json")
    assert result["external_configured"] is False
    assert all(item["ok"] is True for item in result["checks"].values())
    heartbeats.assert_not_called()


def test_cold_probe_failure_never_reports_internal_checks_as_green(tmp_path, runtime, monkeypatch):
    now, _, _, heartbeats = runtime
    monkeypatch.setattr(
        host, "run_probe", Mock(side_effect=ValueError("synthetic private transport error"))
    )
    path = tmp_path / "state.json"
    first = host.tick(config(), path)
    assert all(first["checks"][check]["ok"] is not True for check in CHECKS[:-1])
    assert first["checks"]["host"]["ok"] is True
    now[0] += 60
    second = host.tick(config(), path)
    assert all(second["checks"][check]["ok"] is False for check in CHECKS[:-1])
    assert "private" not in json.dumps(second)
    nonhost_calls = [
        call for call in heartbeats.call_args_list if call.args[0] != config()["urls"]["host"]
    ]
    assert nonhost_calls and all(call.args[1] is False for call in nonhost_calls)


def test_pending_event_persists_until_ack_and_ack_removes_it(tmp_path, runtime):
    now, answer, requests, _ = runtime
    path = tmp_path / "state.json"
    answer["observations"]["telegram_poll"] = {"ok": False, "reason": "poll_stale"}
    first = host.tick(config(), path)
    assert first["telegram_pending"] == 1
    pending = host.load_state(path)["pending"]
    assert set(pending) == {"telegram_poll"}
    assert requests == [{}]
    delivery_id = pending["telegram_poll"]["delivery_id"]
    answer["telegram_delivered"] = [delivery_id, "unrelated-ack"]
    now[0] += 60
    result = host.tick(config(), path)
    assert requests[-1] == pending
    assert result["telegram_pending"] == 0
    assert host.load_state(path)["pending"] == {}


def test_pending_reminder_and_recovery_coalesce_to_latest_event_per_check(tmp_path, runtime):
    now, answer, _, _ = runtime
    path = tmp_path / "state.json"
    answer["observations"]["telegram_poll"] = {"ok": False, "reason": "poll_stale"}
    host.tick(config(), path)
    initial = host.load_state(path)["pending"]["telegram_poll"]
    now[0] += 1800
    host.tick(config(), path)
    reminder = host.load_state(path)["pending"]
    assert set(reminder) == {"telegram_poll"}
    assert reminder["telegram_poll"]["delivery_id"] != initial["delivery_id"]
    assert "Сбой продолжается" in reminder["telegram_poll"]["text"]
    answer["observations"] = healthy()
    now[0] += 60
    host.tick(config(), path)
    assert host.load_state(path)["pending"] == reminder
    now[0] += 60
    host.tick(config(), path)
    recovery = host.load_state(path)["pending"]
    assert set(recovery) == {"telegram_poll"}
    assert "Восстановлено" in recovery["telegram_poll"]["text"]
    assert recovery["telegram_poll"]["delivery_id"] != reminder["telegram_poll"]["delivery_id"]

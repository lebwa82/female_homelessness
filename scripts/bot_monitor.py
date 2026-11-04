"""Host watchdog. Credentials stay in memory; only bounded status is printed."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.monitoring.state import CHECKS, format_telegram_event, transition

PROJECT = "nevidimy-timeweb"
PROBE_NAME = "nevidimy-monitor-probe"
DEFAULT_CONFIG = Path("/etc/nevidimy-monitor.env")
DEFAULT_STATE = Path("/var/lib/nevidimy-monitor/state.json")
URL_PATTERN = re.compile(
    r"https://(?:incidents|uptime)\.betterstack\.com/api/v1/heartbeat/[A-Za-z0-9_-]+"
)


def read_config(path: Path) -> dict:
    values = {}
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("configuration_permissions")
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or key in values:
                raise ValueError("configuration_format")
            values[key] = value
    urls = json.loads(values.get("BOT_MONITOR_HEARTBEAT_URLS_JSON", "{}"))
    if not isinstance(urls, dict) or (urls and set(urls) != set(CHECKS)):
        raise ValueError("heartbeat_configuration_incomplete")
    if any(not isinstance(v, str) or not URL_PATTERN.fullmatch(v) for v in urls.values()):
        raise ValueError("heartbeat_destination_invalid")
    if len(set(urls.values())) != len(urls):
        raise ValueError("heartbeat_destination_duplicate")
    channel = values.get("BOT_MONITOR_TELEGRAM_CHAT_ID", "-1004392684857")
    if not re.fullmatch(r"-100[0-9]{5,16}", channel):
        raise ValueError("telegram_channel_invalid")
    dashboard = values.get("BOT_MONITOR_DASHBOARD_URL", "https://uptime.betterstack.com")
    if not re.fullmatch(r"https://(?:incidents|uptime)\.betterstack\.com(?:/[A-Za-z0-9_/?=&.-]*)?", dashboard):
        raise ValueError("dashboard_destination_invalid")
    return {"urls": urls, "channel": channel, "dashboard": dashboard}


def command(args: list[str], *, timeout: int = 3, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, **kwargs)


def containers() -> dict:
    ids = command([
        "docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
        "--format", "{{.ID}}",
    ])
    if ids.returncode or not ids.stdout.split():
        raise ValueError("containers_unavailable")
    result = command(["docker", "inspect", *ids.stdout.split()])
    if result.returncode:
        raise ValueError("containers_unavailable")
    selected = {}
    for item in json.loads(result.stdout):
        service = item["Config"]["Labels"].get("com.docker.compose.service")
        if service in selected:
            raise ValueError("container_ambiguous")
        selected[service] = item
    return selected


def environment(container: dict) -> dict:
    return dict(value.split("=", 1) for value in container["Config"]["Env"] if "=" in value)


def probe_configuration(items: dict, pending: dict, config: dict) -> tuple[dict, dict]:
    agent = items["agent-bot"]
    ingress = items["telegram-ingress"]
    admin = items["certificate-admin-bot"]
    agent_env, ingress_env, admin_env = map(environment, (agent, ingress, admin))
    networks = set(agent["NetworkSettings"]["Networks"]) & set(
        ingress["NetworkSettings"]["Networks"]
    )
    if len(networks) != 1:
        raise ValueError("network_ambiguous")
    mounts = {m["Destination"]: m["Source"] for m in agent["Mounts"]}
    for dest in ("/run/tls/ca.crt", "/run/tls/ca-bundle.pem"):
        source = Path(mounts[dest])
        if not source.is_absolute() or not source.is_file():
            raise ValueError("trust_configuration_invalid")
    image = agent["Image"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("unpinned_image")
    docker_env = dict(os.environ)
    docker_env.update({
        "MONITOR_IMAGE": image,
        "MONITOR_NETWORK": next(iter(networks)),
        "MONITOR_PROBE_SOURCE": str(ROOT / "scripts/bot_monitor_probe.py"),
        "MONITOR_CA_SOURCE": mounts["/run/tls/ca.crt"],
        "MONITOR_CA_BUNDLE_SOURCE": mounts["/run/tls/ca-bundle.pem"],
    })
    data = {
        "redis_url": ingress_env["TELEGRAM_INGRESS_REDIS_URL"],
        "account_id": int(agent_env["CHATWOOT_ACCOUNT_ID"]),
        "telegram_bot_token": ingress_env["TELEGRAM_BOT_TOKEN"],
        "proxy_url": "http://telegram-proxy:3128",
        "agent_health_url": "https://agent-bot:8080/healthz",
        "ca_file": "/run/tls/ca-bundle.pem",
        "telegram_alert_token": admin_env["CERTIFICATE_ADMIN_BOT_TOKEN"],
        "telegram_alert_chat_id": config["channel"],
        "telegram_events": [
            {"event_id": value["delivery_id"], "text": value["text"]}
            for value in pending.values()
        ],
    }
    return data, docker_env


def run_probe(data: dict, docker_env: dict) -> dict:
    args = [sys.executable, str(ROOT / "scripts/chatwoot_compose.py"), "--monitoring",
            "run", "--rm", "--no-deps", "--name", PROBE_NAME, "-T", "probe"]
    try:
        result = command(args, timeout=27, input=json.dumps(data), env=docker_env)
        if result.returncode or len(result.stdout) > 32768:
            raise ValueError("probe_failed")
        answer = json.loads(result.stdout)
        if not isinstance(answer, dict) or not isinstance(answer.get("observations"), dict):
            raise ValueError("probe_failed")  # noqa: TRY004 - bounded probe contract
        return answer
    except subprocess.TimeoutExpired:
        command(["docker", "rm", "-f", PROBE_NAME], timeout=3)
        raise ValueError("probe_timeout") from None


def heartbeat(url: str, ok: bool) -> bool:
    # Direct HTTPS to the external watchdog is independent of Telegram's proxy.
    request = Request(url + ("" if ok else "/fail"), data=b"", method="POST")
    try:
        opener = build_opener(ProxyHandler({}))
        # No redirects: a secret heartbeat URL must never be forwarded elsewhere.
        from urllib.request import HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener.add_handler(NoRedirect())
        with opener.open(request, timeout=3) as response:
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - never log a secret URL
        return False


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_state(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("state_invalid")
    if not path.exists():
        return {"version": 1, "machine": {}, "pending": {}}
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 131072:
        raise ValueError("state_invalid")
    state = json.loads(path.read_text())
    if (not isinstance(state, dict) or set(state) != {"version", "machine", "pending"}
            or type(state["version"]) is not int or state["version"] != 1
            or not isinstance(state["pending"], dict)
            or not set(state["pending"]).issubset(CHECKS)):
        raise ValueError("state_invalid")
    for item in state["pending"].values():
        if (not isinstance(item, dict) or set(item) != {"delivery_id", "text"}
                or not isinstance(item["delivery_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", item["delivery_id"])
                or not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 4000):
            raise ValueError("state_invalid")
    # Validate machine even when the network/configuration is unavailable.
    transition(state["machine"], {}, time.time())
    return state


def backlog_observation(observation: dict, path: Path, now: float, *, persist: bool) -> dict:
    """Track observed nonempty queues without reading jobs or claiming exact job age."""
    if not isinstance(observation, dict):
        return {"ok": False, "reason": "queue_metadata_invalid"}
    if observation.get("ok") is not True:
        return observation
    ready = observation.get("ready_counts")
    unread = observation.get("stream_unread_counts")
    if not isinstance(ready, dict) or not isinstance(unread, dict) or len(ready) > 64 or len(unread) > 8:
        return {"ok": False, "reason": "queue_metadata_invalid"}
    if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", k) for k in ready):
        return {"ok": False, "reason": "queue_metadata_invalid"}
    if any(str(k) not in set("01234567") for k in unread):
        return {"ok": False, "reason": "queue_metadata_invalid"}
    counts = {**{"sidekiq:" + k: v for k, v in ready.items()},
              **{"stream:" + str(k): v for k, v in unread.items()}}
    if any(type(v) is not int or v < 0 for v in counts.values()):
        return {"ok": False, "reason": "queue_metadata_invalid"}
    previous = {}
    if path.is_symlink():
        return {"ok": False, "reason": "queue_metadata_invalid"}
    try:
        if path.exists():
            if path.stat().st_mode & 0o077 or path.stat().st_size > 32768:
                return {"ok": False, "reason": "queue_metadata_invalid"}
            previous = json.loads(path.read_text())
            if not isinstance(previous, dict) or len(previous) > 72:
                return {"ok": False, "reason": "queue_metadata_invalid"}
            if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= now
                   for v in previous.values()):
                return {"ok": False, "reason": "queue_metadata_invalid"}
        current = {key: previous.get(key, now) for key, n in counts.items() if n > 0}
        age = max((now - stamp for stamp in current.values()), default=0)
        if persist:
            save_state(path, current)
        if age > 180:
            return {"ok": False, "reason": "queue_backlog"}
        return observation
    except (OSError, ValueError, TypeError, OverflowError):
        return {"ok": False, "reason": "queue_metadata_invalid"}


def tick(config: dict, path: Path, *, dry_run: bool = False, test_telegram: bool = False) -> dict:
    state = load_state(path)
    if test_telegram:
        pending = {"host": {"delivery_id": "monitor-setup-test", "text": (
            "Проверка мониторинга Невидимого фонда. Канал уведомлений подключён.\n"
            "Email остаётся основным каналом при сбое сервера или Telegram."
        )}}
    else:
        pending = {} if dry_run else state["pending"]
    host_ok = None
    if not dry_run and not test_telegram and config["urls"]:
        host_ok = heartbeat(config["urls"]["host"], True)
    items = {}
    try:
        items = containers()
        data, docker_env = probe_configuration(items, pending, config)
        answer = run_probe(data, docker_env)
        observations = answer["observations"]
    except Exception as error:  # noqa: BLE001 - subprocess/SDK details can contain credentials
        reason = "probe_timeout" if str(error) == "probe_timeout" else "probe_failed"
        answer = {"telegram_delivered": []}
        observations = {check: {"ok": False, "reason": reason} for check in CHECKS[:-1]}
    observations["host"] = {"ok": True, "reason": "host_ok"}
    if test_telegram:
        return {"test_telegram_delivered": "monitor-setup-test" in answer.get("telegram_delivered", [])}
    if items.get("agent-bot", {}).get("State", {}).get("Status") != "running":
        observations["agent"] = {"ok": False, "reason": "container_stopped"}
    tunnel = command(["systemctl", "is-active", "nevidimy-proxy-tunnel.service"])
    if tunnel.returncode:
        observations["telegram_proxy"] = {"ok": False, "reason": "tunnel_unavailable"}
    now = time.time()
    observations["queues"] = backlog_observation(
        observations.get("queues", {}), path.with_name("queue-backlog.json"), now,
        persist=not dry_run,
    )
    machine, events, reported = transition(state["machine"], observations, now)
    delivered = set(answer.get("telegram_delivered", []))
    remaining = {k: v for k, v in state["pending"].items() if v["delivery_id"] not in delivered}
    for event in events:
        delivery_id = hashlib.sha256(
            f"{event['event_id']}:{event['kind']}:{int(event['at'])}".encode()
        ).hexdigest()
        remaining[event["check"]] = {
            "delivery_id": delivery_id,
            "text": format_telegram_event(event, config["dashboard"]),
        }
    external_ok = {"host": host_ok}
    if not dry_run:
        # Persist before network writes; Telegram retries coalesce per component.
        save_state(path, {"version": 1, "machine": machine, "pending": remaining})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                check: executor.submit(heartbeat, config["urls"][check], item["ok"])
                for check, item in reported.items()
                if check != "host" and config["urls"] and type(item["ok"]) is bool
            }
            external_ok.update({check: future.result() for check, future in futures.items()})
    return {"external_configured": bool(config["urls"]), "checks": reported,
            "heartbeat_delivery": external_ok, "telegram_pending": len(remaining),
            "events": [{"check": e["check"], "kind": e["kind"]} for e in events]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-telegram", action="store_true")
    args = parser.parse_args()
    try:
        if args.dry_run and args.test_telegram:
            raise ValueError("conflicting_modes")
        if Path("/etc/nevidimy-deployment-target").read_text().strip() != "timeweb-nevidimy-prod-app-msk-1":
            raise ValueError("deployment_target_mismatch")
        args.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (args.state.parent / "monitor.lock").open("w") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = tick(read_config(args.config), args.state, dry_run=args.dry_run,
                          test_telegram=args.test_telegram)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if not args.test_telegram or result["test_telegram_delivered"] else 1
    except Exception as error:  # noqa: BLE001 - sanitize all external errors
        print(json.dumps({"monitor_error": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def compose_command() -> list[str]:
    binary = os.environ.get("DOCKER_COMPOSE_TEST_BINARY")
    candidates = [[binary]] if binary else []
    if shutil.which("docker"):
        candidates.append(["docker", "compose"])
    if shutil.which("docker-compose"):
        candidates.append(["docker-compose"])
    for command in candidates:
        result = subprocess.run(
            [*command, "version"], capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode == 0 and "Docker Compose version" in result.stdout:
            return command
    pytest.skip("Docker Compose 2.24.4+ is required to validate the managed override")


def render(
    compose_command, *, managed: bool, missing_host: bool = False,
    timeweb=False, http_local=False, telegram=False, ip_https=False, missing_ip=False,
    domain_https=False,
):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")
    }
    environment.update(
        CHATWOOT_HOSTNAME="chatwoot.example.test",
        AGENT_HOSTNAME="agent.example.test",
        POSTGRES_HOST="database.example.test",
        POSTGRES_PORT="6543",
        POSTGRES_DATABASE="default_db",
        POSTGRES_USERNAME="cloud_user",
        POSTGRES_PASSWORD="test-only-not-a-real-password",
        CHATWOOT_IMAGE="docker.io/chatwoot/chatwoot:timeweb-verified-test",
        TELEGRAM_HTTP_PROXY="http://telegram-proxy:3128" if telegram else "",
        CHATWOOT_PUBLIC_IP="192.0.2.10",
    )
    if missing_ip:
        environment.pop("CHATWOOT_PUBLIC_IP")
    if missing_host:
        environment.pop("POSTGRES_HOST")
    command = [
        *compose_command,
        "--project-directory",
        str(ROOT / "deploy/chatwoot"),
        "--env-file",
        os.devnull,
        "-f",
        str(ROOT / "deploy/chatwoot/compose.yml"),
    ]
    if managed:
        command.extend(["-f", str(ROOT / "deploy/chatwoot/compose.managed.yml")])
    if timeweb:
        command.extend(["-f", str(ROOT / "deploy/timeweb/compose.yml")])
    if http_local:
        command.extend(["-f", str(ROOT / "deploy/timeweb/compose.http-local.yml")])
    if ip_https:
        command.extend(["-f", str(ROOT / "deploy/timeweb/compose.ip-https.yml")])
    if domain_https:
        command.extend(["-f", str(ROOT / "deploy/timeweb/compose.domain-https.yml")])
    profiles = ("agent", "certificate-admin") if timeweb else (
        "agent", "certificate-admin", "polling"
    )
    for profile in profiles:
        command.extend(["--profile", profile])
    if telegram:
        command.extend(["--profile", "telegram", "--profile", "polling"])
    command.extend(
        [
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ]
    )
    # Config-only checks use synthetic test env files instead of VM credentials.
    # Production Compose files and their required-file semantics stay intact.
    with tempfile.TemporaryDirectory(prefix="chatwoot-compose-test-") as directory:
        for index, argument in enumerate(command):
            if index and command[index - 1] == "-f":
                source = Path(argument).read_text()
                def replace_env(match):
                    path = Path(directory) / Path(match.group(2)).name
                    marker = path.stem.replace("-", "_").upper()
                    path.write_text(f"COMPOSE_ENV_PROBE_{marker}=1\n")
                    return f"{match.group(1)}- {path}"

                source = re.sub(r"(?m)^(\s*)- (/etc/women-help-[\w-]+\.env)$", replace_env, source)
                copy = Path(directory) / f"{index}.yml"
                copy.write_text(source)
                command[index] = str(copy)
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )


def test_default_compose_keeps_local_postgres(compose_command) -> None:
    result = render(compose_command, managed=False)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "postgres" in services
    for name in ("chatwoot", "sidekiq"):
        assert services[name]["environment"]["POSTGRES_HOST"] == "postgres"
        assert "postgres" in services[name]["depends_on"]


def test_managed_compose_removes_local_postgres_and_shares_database(compose_command) -> None:
    result = render(compose_command, managed=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "postgres" not in services
    for name in ("chatwoot", "sidekiq"):
        environment = services[name]["environment"]
        assert environment["POSTGRES_HOST"] == "database.example.test"
        assert environment["POSTGRES_PORT"] == "6543"
        assert environment["POSTGRES_DATABASE"] == "default_db"
        assert environment["POSTGRES_USERNAME"] == "cloud_user"
        assert environment["PGSSLMODE"] == "verify-full"
        assert set(services[name]["depends_on"]) == {"redis"}
        assert services[name]["depends_on"]["redis"]["condition"] == "service_healthy"
        assert services[name]["volumes"]
    for name in ("agent-bot", "certificate-admin-bot"):
        environment = services[name]["environment"]
        assert environment["CERTIFICATE_DATABASE_HOST"] == "database.example.test"
        assert environment["CERTIFICATE_DATABASE_PORT"] == "6543"
        assert environment["CERTIFICATE_DATABASE_NAME"] == "default_db"
        assert environment["CERTIFICATE_DATABASE_USER"] == "cloud_user"
        assert environment["CERTIFICATE_DATABASE_SSLMODE"] == "verify-full"
    assert "healthcheck" in services["agent-bot"]
    assert "test-only-not-a-real-password" not in result.stdout
    assert "POSTGRES_PASSWORD" not in services["chatwoot"]["environment"]


def test_managed_compose_fails_closed_when_database_host_is_missing(compose_command) -> None:
    result = render(compose_command, managed=True, missing_host=True)
    assert result.returncode != 0
    assert "Set the managed PostgreSQL host" in result.stderr


def test_timeweb_ip_https_publishes_only_tls_proxy_and_preserves_internal_services(
    compose_command,
) -> None:
    result = render(compose_command, managed=True, timeweb=True, ip_https=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "postgres" not in services
    assert "telegram-ingress" not in services
    for name, service in services.items():
        if name != "caddy":
            assert not service.get("ports")
    caddy = services["caddy"]
    assert {(p["host_ip"], p["published"], p["target"]) for p in caddy["ports"]} == {
        ("0.0.0.0", "80", 80), ("0.0.0.0", "443", 443),
    }
    assert caddy["environment"]["CHATWOOT_PUBLIC_IP"] == "192.0.2.10"
    assert not caddy.get("env_file")
    assert {v["target"] for v in caddy["volumes"]} == {
        "/etc/caddy/Caddyfile", "/data", "/config",
    }
    for name in ("chatwoot", "sidekiq"):
        service = services[name]
        assert service["environment"]["FRONTEND_URL"] == "https://192.0.2.10"
        assert service["environment"]["FORCE_SSL"] == "true"
        assert service["environment"]["PGSSLMODE"] == "verify-full"
        mount = next(v for v in service["volumes"]
                     if v["target"] == "/app/config/initializers/nevidimy_tls_offload.rb")
        assert mount["read_only"] is True
        assert mount["bind"]["create_host_path"] is False


def test_timeweb_ip_https_requires_current_ip_and_automates_public_certificates(
    compose_command,
) -> None:
    result = render(compose_command, managed=True, timeweb=True, ip_https=True, missing_ip=True)
    assert result.returncode != 0
    assert "Set the current Timeweb VM IPv4" in result.stderr
    caddy = (ROOT / "deploy/timeweb/Caddyfile.ip-https").read_text()
    assert "https://{$CHATWOOT_PUBLIC_IP}" in caddy
    assert "default_sni {$CHATWOOT_PUBLIC_IP}" in caddy
    assert "profile shortlived" in caddy
    assert "dir https://acme-v02.api.letsencrypt.org/directory" in caddy
    assert "redir https://{$CHATWOOT_PUBLIC_IP}{uri} 308" in caddy
    assert "agent-bot" not in caddy
    assert "tls internal" not in caddy
    unit = (ROOT / "deploy/timeweb/ip-https.conf").read_text()
    assert unit.count("-f deploy/timeweb/compose.ip-https.yml") == 2
    assert "compose.http-local.yml" not in unit
    assert "stop caddy sidekiq chatwoot redis" in unit
    assert "telegram" not in unit


def test_timeweb_tls_offload_keeps_native_ssl_protection() -> None:
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the TLS offload initializer probe")
    probe = """
require 'ostruct'
module Rails
  def self.application
    @app ||= OpenStruct.new(config: OpenStruct.new(force_ssl: true, assume_ssl: false))
  end
end
load ARGV.fetch(0)
abort 'TLS offload missing' unless Rails.application.config.assume_ssl
abort 'SSL disabled' unless Rails.application.config.force_ssl
"""
    result = subprocess.run(
        [ruby, "-e", probe, str(ROOT / "deploy/timeweb/tls_offload.rb")],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_timeweb_domain_https_exposes_only_chatwoot(compose_command) -> None:
    result = render(compose_command, managed=True, timeweb=True, domain_https=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "postgres" not in services
    assert "telegram-ingress" not in services
    for name, service in services.items():
        if name != "caddy":
            assert not service.get("ports")
    caddy = services["caddy"]
    assert {(p["host_ip"], p["published"], p["target"]) for p in caddy["ports"]} == {
        ("0.0.0.0", "80", 80), ("0.0.0.0", "443", 443),
    }
    assert not caddy.get("env_file")
    certificate_mount = next(v for v in caddy["volumes"]
                             if v["target"] == "/run/certs/chatwoot")
    assert certificate_mount["read_only"] is True
    assert certificate_mount["bind"].get("create_host_path", False) is False
    for name in ("chatwoot", "sidekiq"):
        service = services[name]
        assert service["environment"]["FRONTEND_URL"] == "https://chatwoot.example.test"
        assert service["environment"]["FORCE_SSL"] == "true"
        assert service["environment"]["PGSSLMODE"] == "verify-full"
        assert any(v["target"] == "/app/config/initializers/nevidimy_tls_offload.rb"
                   and v["read_only"] for v in service["volumes"])


def test_timeweb_domain_https_has_no_plaintext_ui_or_public_agent() -> None:
    caddy = (ROOT / "deploy/timeweb/Caddyfile.domain-https").read_text()
    assert "https://{$CHATWOOT_HOSTNAME}" in caddy
    assert "tls /run/certs/chatwoot/fullchain.pem /run/certs/chatwoot/privkey.pem" in caddy
    assert "redir @chatwoot https://{$CHATWOOT_HOSTNAME}{uri} 308" in caddy
    assert "respond 404" in caddy
    assert 'header >Strict-Transport-Security "max-age=31536000"' in caddy
    assert "agent-bot" not in caddy
    assert "tls internal" not in caddy
    assert "includeSubDomains" not in caddy
    unit = (ROOT / "deploy/timeweb/domain-https.conf").read_text()
    assert unit.count("scripts/chatwoot_compose.py") == 2
    assert "compose.http-local.yml" not in unit
    assert "telegram" not in unit


def test_timeweb_mounts_ca_and_does_not_enable_telegram(compose_command) -> None:
    result = render(compose_command, managed=True, timeweb=True)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["name"] == "nevidimy-timeweb"
    services = config["services"]
    assert "postgres" not in services
    for name in ("chatwoot", "sidekiq", "agent-bot", "certificate-admin-bot"):
        service = services[name]
        assert service["environment"]["PGSSLROOTCERT"] == "/run/certs/postgresql-ca.crt"
        ca = next(v for v in service["volumes"] if v["target"] == "/run/certs/postgresql-ca.crt")
        assert ca["read_only"] is True
        assert ca["bind"].get("create_host_path", False) is False
        assert ca["source"] == "/opt/nevidimy/certs/postgresql-ca.crt"
    for name in ("chatwoot", "sidekiq"):
        assert services[name]["environment"]["PGSSLMODE"] == "verify-full"
        assert services[name]["environment"]["http_proxy"] == ""
        assert services[name]["environment"]["https_proxy"] == ""
        assert services[name]["pull_policy"] == "never"
        initializer = next(v for v in services[name]["volumes"]
                           if v["target"] == "/app/config/initializers/nevidimy_agent_webhook.rb")
        assert initializer["read_only"] is True
        assert initializer["bind"].get("create_host_path", False) is False
        assert initializer["source"] == str(ROOT / "deploy/timeweb/agent_webhook.rb")
        for filename, target in (
            ("telegram_avatar.rb", "/app/config/initializers/nevidimy_telegram_avatar.rb"),
            ("telegram_avatar_job.rb", "/app/app/jobs/nevidimy_telegram_avatar_job.rb"),
            ("telegram_callback.rb", "/app/config/initializers/nevidimy_telegram_callback.rb"),
            ("atomic_turn.rb", "/app/config/initializers/nevidimy_atomic_turn.rb"),
            ("telegram_transport.rb", "/app/config/initializers/nevidimy_telegram_transport.rb"),
            ("turn_timing.rb", "/app/config/initializers/nevidimy_turn_timing.rb"),
            ("webhook_presenter_fast_path.rb", "/app/config/initializers/nevidimy_webhook_presenter_fast_path.rb"),
            ("agent_bot_self_event_filter.rb", "/app/config/initializers/nevidimy_agent_bot_self_event_filter.rb"),
            ("interactive_jobs.rb", "/app/config/initializers/nevidimy_interactive_jobs.rb"),
        ):
            mount = next(v for v in services[name]["volumes"] if v["target"] == target)
            assert mount["read_only"] is True
            assert mount["bind"].get("create_host_path", False) is False
            assert mount["source"] == str(ROOT / "deploy/timeweb" / filename)
    assert services["chatwoot"]["environment"]["ENABLE_ACCOUNT_SIGNUP"] == "false"
    assert int(services["agent-bot"]["mem_limit"]) == 768 * 1024 * 1024
    healthcheck = services["chatwoot"]["healthcheck"]["test"]
    assert "X-Forwarded-Proto" in healthcheck[-1]
    assert "'https'" in healthcheck[-1]
    assert not services["caddy"].get("env_file")
    assert set(services["caddy"]["environment"]) == {"CHATWOOT_HOSTNAME", "AGENT_HOSTNAME"}
    assert "telegram-proxy" not in services
    assert "telegram-ingress" not in services
    for name, service in services.items():
        if name != "caddy":
            assert "ports" not in service


def test_timeweb_telegram_is_explicit_and_uses_minimal_credentials(compose_command) -> None:
    result = render(compose_command, managed=True, timeweb=True, http_local=True, telegram=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("chatwoot", "sidekiq"):
        assert services[name]["environment"]["http_proxy"] == "http://telegram-proxy:3128"
        assert services[name]["environment"]["https_proxy"] == "http://telegram-proxy:3128"
        assert services[name]["environment"]["PGSSLMODE"] == "verify-full"
    # Some Compose builds resolve env_file even with --no-env-resolution.
    # Distinct synthetic markers still verify that !override excludes agent env.
    proxy_files = services["telegram-proxy"].get("env_file")
    if proxy_files is not None:
        assert [Path(entry["path"]).name for entry in proxy_files] == ["women-help-telegram-proxy.env"]
    else:
        markers = {key: value for key, value in services["telegram-proxy"]["environment"].items()
                   if key.startswith("COMPOSE_ENV_PROBE_")}
        assert markers == {"COMPOSE_ENV_PROBE_WOMEN_HELP_TELEGRAM_PROXY": "1"}
    proxy = services["telegram-proxy"]
    assert proxy["environment"]["TELEGRAM_PROXY_SSH_TUNNEL"] == "true"
    mount = next(v for v in proxy["volumes"] if v["target"] == "/run/nevidimy-proxy")
    assert mount["source"] == "/opt/nevidimy/run/proxy-tunnel"
    assert mount["read_only"] is True
    assert mount["bind"].get("create_host_path", False) is False
    assert services["telegram-ingress"]["environment"]["TELEGRAM_PROXY_URL"] == "http://telegram-proxy:3128"
    assert services["telegram-ingress"]["depends_on"]["telegram-proxy"]["condition"] == "service_started"
    assert int(services["telegram-ingress"]["mem_limit"]) == 256 * 1024 * 1024
    assert services["telegram-ingress"]["healthcheck"]["test"][-1] == "app.chatwoot.telegram_healthcheck"
    for name in ("telegram-proxy", "telegram-ingress"):
        assert not services[name].get("ports")
        assert services[name]["restart"] == "unless-stopped"


def test_timeweb_telegram_unit_starts_only_explicit_transport() -> None:
    content = (ROOT / "deploy/timeweb/nevidimy-telegram.service").read_text()
    assert "nevidimy-agent.service" in content
    assert "ExecStartPre=/usr/bin/test -s /etc/women-help-telegram.env" in content
    start = next(line for line in content.splitlines() if line.startswith("ExecStart="))
    assert "--profile telegram --profile polling" in start
    assert start.endswith("telegram-proxy telegram-ingress")
    stop = next(line for line in content.splitlines() if line.startswith("ExecStop="))
    assert stop.endswith("stop telegram-ingress telegram-proxy")
    assert "--no-deps" in start and "--no-build" in start and "--pull never" in start


def test_http_local_publishes_only_loopback_and_keeps_database_tls(compose_command) -> None:
    result = render(compose_command, managed=True, timeweb=True, http_local=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    caddy = services["caddy"]
    assert len(caddy["ports"]) == 1
    port = caddy["ports"][0]
    assert (port["host_ip"], str(port["published"]), port["target"]) == (
        "127.0.0.1", "18080", 80,
    )
    config = next(v for v in caddy["volumes"] if v["target"] == "/etc/caddy/Caddyfile")
    assert config["source"] == str(ROOT / "deploy/timeweb/Caddyfile.http-local")
    assert config["read_only"] is True
    for name in ("chatwoot", "sidekiq"):
        environment = services[name]["environment"]
        assert environment["FORCE_SSL"] == "false"
        assert environment["FRONTEND_URL"] == "http://127.0.0.1:18080"
        assert environment["PGSSLMODE"] == "verify-full"
    assert services["chatwoot"]["environment"]["ENABLE_ACCOUNT_SIGNUP"] == "false"
    for name in ("agent-bot", "certificate-admin-bot"):
        assert services[name]["environment"]["CERTIFICATE_DATABASE_SSLMODE"] == "verify-full"
    assert "postgres" not in services
    assert "telegram-proxy" not in services
    assert "telegram-ingress" not in services
    for name, service in services.items():
        if name != "caddy":
            assert "ports" not in service


def test_http_local_caddy_has_no_public_agent_route_or_https_redirect() -> None:
    configuration = (ROOT / "deploy/timeweb/Caddyfile.http-local").read_text()
    assert "auto_https off" in configuration
    assert "reverse_proxy chatwoot:3000" in configuration
    assert "agent-bot" not in configuration
    assert "redir" not in configuration


def test_http_local_systemd_drop_in_preserves_target_guard() -> None:
    base = (ROOT / "deploy/timeweb/nevidimy.service").read_text()
    override = (ROOT / "deploy/timeweb/http-local.conf").read_text()
    assert "ExecStartPre=/usr/bin/grep -Fxq timeweb-nevidimy-prod-app-msk-1" in base
    assert "ExecStartPre=" not in override
    start = next(line for line in override.splitlines() if line.startswith("ExecStart=/"))
    assert "scripts/chatwoot_compose.py" in start
    assert "--no-build --pull never --wait" in start
    assert start.endswith("redis chatwoot sidekiq caddy")
    assert "--profile" not in start


def test_timeweb_agent_unit_starts_only_agent_after_core_with_target_guard() -> None:
    unit = (ROOT / "deploy/timeweb/nevidimy-agent.service").read_text()
    assert "Requires=docker.service nevidimy.service" in unit
    assert "After=docker.service nevidimy.service" in unit
    assert "ExecStartPre=/usr/bin/grep -Fxq timeweb-nevidimy-prod-app-msk-1" in unit
    assert "ExecStartPre=/usr/bin/test -s /etc/women-help-agent.env" in unit
    start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    stop = next(line for line in unit.splitlines() if line.startswith("ExecStop="))
    assert "--profile agent up -d --no-build --pull never --no-deps --wait" in start
    assert start.endswith("agent-bot")
    assert stop.endswith("--profile agent stop agent-bot")
    for line in (start, stop):
        assert "scripts/chatwoot_compose.py" in line
        assert "telegram" not in line
    assert "WantedBy=multi-user.target" in unit


def test_internal_agent_webhook_exception_is_exact_signed_and_has_no_redirects() -> None:
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required to exercise the Chatwoot initializer")
    result = subprocess.run([ruby, str(ROOT / "tests/timeweb_agent_webhook_probe.rb")],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout).values())

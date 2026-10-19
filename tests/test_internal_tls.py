import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest
from redis.asyncio import Redis

from app.transport_tls import server_context
from scripts.prepare_internal_tls import SERVICES, prepare

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("internal-tls")
    prepare(directory)
    return directory


def test_internal_ca_keys_and_public_roots_are_kept_separate(certificates):
    assert (certificates / "ca.key").stat().st_mode & 0o777 == 0o600
    assert (certificates / "ca.crt").stat().st_mode & 0o777 == 0o644
    assert (certificates / "ca-bundle.pem").stat().st_mode & 0o777 == 0o644
    bundle = (certificates / "ca-bundle.pem").read_text()
    assert "PRIVATE KEY" not in bundle
    assert bundle.count("BEGIN CERTIFICATE") > 1
    assert (certificates / "ca.crt").read_text() in bundle
    for service in SERVICES:
        assert (certificates / f"{service}.key").stat().st_mode & 0o777 == 0o600
        assert (certificates / f"{service}.crt").stat().st_mode & 0o777 == 0o644


def test_partial_or_invalid_tls_configuration_never_falls_back_to_http(tmp_path):
    assert server_context("", "") is None
    with pytest.raises(ValueError):
        server_context("missing.crt", "")
    with pytest.raises((OSError, ssl.SSLError)):
        server_context(str(tmp_path / "missing.crt"), str(tmp_path / "missing.key"))


def test_real_https_checks_ca_hostname_and_rejects_plaintext(certificates):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = server_context(
        str(certificates / "agent-bot.crt"), str(certificates / "agent-bot.key")
    )
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.server_address
    trusted = ssl.create_default_context(cafile=str(certificates / "ca.crt"))
    try:
        with urlopen(f"https://127.0.0.1:{address[1]}", context=trusted, timeout=3) as response:
            assert response.read() == b"ok"
        with (
            socket.create_connection(address, timeout=3) as connection,
            pytest.raises(ssl.SSLCertVerificationError),
        ):
            trusted.wrap_socket(connection, server_hostname="wrong-service")
        with (
            socket.create_connection(address, timeout=3) as connection,
            pytest.raises(ssl.SSLCertVerificationError),
        ):
            ssl.create_default_context().wrap_socket(connection, server_hostname="agent-bot")
        with pytest.raises(OSError):
            urlopen(f"http://127.0.0.1:{address[1]}", timeout=3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_renewal_preserves_ca_and_replaces_leaf_certificates(tmp_path):
    prepare(tmp_path)
    ca = (tmp_path / "ca.crt").read_bytes()
    leaf = (tmp_path / "agent-bot.crt").read_bytes()
    with pytest.raises(ValueError):
        prepare(tmp_path)
    prepare(tmp_path, renew=True)
    assert (tmp_path / "ca.crt").read_bytes() == ca
    assert (tmp_path / "agent-bot.crt").read_bytes() != leaf


def test_certificate_issuance_refuses_an_unrelated_directory(tmp_path):
    existing = tmp_path / "keep.txt"
    existing.write_text("preserve")
    with pytest.raises(ValueError):
        prepare(tmp_path)
    assert existing.read_text() == "preserve"
    assert not (tmp_path / ".gitignore").exists()
    assert not (tmp_path / "ca.key").exists()


@pytest.mark.parametrize(
    ("managed", "timeweb", "http_local"),
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
def test_compose_tls_is_explicit_verified_and_does_not_publish_internal_ports(
    tmp_path, certificates, managed, timeweb, http_local
):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required")
    empty_env = tmp_path / "empty.env"
    empty_env.touch()
    compose_files = ["compose.yml"]
    if managed:
        compose_files.append("compose.managed.yml")
    if timeweb:
        compose_files.append("../timeweb/compose.yml")
    if http_local:
        compose_files.append("../timeweb/compose.http-local.yml")
    compose_files.append("compose.tls.yml")
    if http_local:
        compose_files.append("../timeweb/compose.tls-http-local.yml")
    if not managed:
        compose_files.append("compose.tls-postgres.yml")
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT / "deploy/chatwoot"),
        "--env-file",
        str(empty_env),
        "--profile",
        "agent",
        "--profile",
        "polling",
        "--profile",
        "certificate-admin",
    ]
    if timeweb:
        command.extend(["--profile", "telegram"])
    for index, name in enumerate(compose_files):
        source = (ROOT / "deploy/chatwoot" / name).read_text()
        for env_name in ("chatwoot", "agent", "telegram", "telegram-proxy"):
            source = source.replace(f"/etc/women-help-{env_name}.env", str(empty_env))
        path = tmp_path / f"{index}-{Path(name).name}"
        path.write_text(source)
        command.extend(["-f", str(path)])
    provider_ca = tmp_path / "provider-ca.crt"
    provider_ca.write_text("synthetic provider CA path for config rendering only")
    postgres_ca = provider_ca if managed else certificates / "ca.crt"
    environment = {
        **os.environ,
        "INTERNAL_TLS_DIR": str(certificates),
        "POSTGRES_TLS_CA_FILE": str(postgres_ca),
        "CHATWOOT_HOSTNAME": "chatwoot.example.test",
        "AGENT_HOSTNAME": "agent.example.test",
        "POSTGRES_HOST": "managed.example.test",
        "POSTGRES_DATABASE": "chatwoot",
        "POSTGRES_USERNAME": "chatwoot",
        "CHATWOOT_IMAGE": "docker.io/chatwoot/chatwoot:tls-config-test",
    }
    rendered = subprocess.run(
        [*command, "config", "--no-env-resolution", "--format", "json"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    assert ("postgres" not in services) == managed
    if http_local:
        ports = services["caddy"]["ports"]
        assert len(ports) == 1
        assert ports[0]["host_ip"] == "127.0.0.1"
        assert str(ports[0]["published"]) == "18080"
        caddyfile = next(
            v for v in services["caddy"]["volumes"] if v["target"] == "/etc/caddy/Caddyfile"
        )
        assert caddyfile["source"].endswith("/timeweb/Caddyfile.tls-http-local")
    for name in ("chatwoot", "sidekiq"):
        env = services[name]["environment"]
        assert env["PGSSLMODE"] == "verify-full"
        assert env["REDIS_URL"].startswith("rediss://")
        assert env["REDIS_OPENSSL_VERIFY_MODE"] == "peer"
        database_ca = next(
            v for v in services[name]["volumes"] if v["target"] == env["PGSSLROOTCERT"]
        )
        assert database_ca["source"] == str(postgres_ca)
    for name in ("agent-bot", "telegram-ingress"):
        env = services[name]["environment"]
        assert env["CHATWOOT_BASE_URL"] == "https://chatwoot:3000"
        client = Redis.from_url(env["TELEGRAM_INGRESS_REDIS_URL"])
        options = client.connection_pool.connection_kwargs
        assert options["ssl_check_hostname"] is True
        assert options["ssl_cert_reqs"] == "required"
    assert services["agent-bot"]["environment"]["CERTIFICATE_DATABASE_SSLMODE"] == "verify-full"
    redis_command = services["redis"]["command"]
    assert redis_command[redis_command.index("--port") + 1] == "0"
    for name, service in services.items():
        if name != "caddy":
            assert not service.get("ports")
        assert not any(
            str(volume.get("source", "")).endswith("/ca.key")
            for volume in service.get("volumes", [])
        )

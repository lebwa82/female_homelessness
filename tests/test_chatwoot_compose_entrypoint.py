from pathlib import Path

import pytest

from scripts import chatwoot_compose

ROOT = Path(__file__).resolve().parents[1]


def tls_environment(tmp_path, monkeypatch):
    directory = tmp_path / "tls"
    directory.mkdir()
    for name in ("ca.crt", "ca-bundle.pem"):
        (directory / name).write_text("public-test-certificate")
    for service in ("chatwoot", "agent-bot", "redis"):
        for suffix in ("crt", "key"):
            path = directory / f"{service}.{suffix}"
            path.write_text("synthetic-test-file")
            path.chmod(0o600)
    provider_ca = tmp_path / "provider-ca.crt"
    provider_ca.write_text("public-provider-test-certificate")
    monkeypatch.setattr(chatwoot_compose, "POSTGRES_CA", provider_ca)
    monkeypatch.setattr(chatwoot_compose, "HTTP_LOCAL_DROPIN", tmp_path / "http-local.conf")
    environment = tmp_path / "platform.env"
    environment.write_text(
        f"CHATWOOT_INTERNAL_TLS=1\nINTERNAL_TLS_DIR={directory}\n"
        f"POSTGRES_TLS_CA_FILE={provider_ca}\n"
        f"NEVIDIMY_AGENT_WEBHOOK_URL=https://agent-bot:8080/webhooks/chatwoot/agent/{'x' * 43}\n"
        "SECRET_KEY_BASE=synthetic-value-that-must-not-be-logged\n"
    )
    return environment, directory


def test_timeweb_units_and_http_dropin_use_one_persistent_compose_selection():
    for name in (
        "nevidimy.service",
        "nevidimy-agent.service",
        "nevidimy-telegram.service",
        "http-local.conf",
    ):
        unit = ROOT / "deploy/timeweb" / name
        commands = [
            line
            for line in unit.read_text().splitlines()
            if line.startswith(("ExecStart=/", "ExecStop=/"))
        ]
        assert len(commands) == 2
        assert all("scripts/chatwoot_compose.py" in line for line in commands)


@pytest.mark.parametrize("http_local", [False, True])
def test_repeated_invocations_keep_timeweb_tls_and_ui_mode(tmp_path, monkeypatch, http_local):
    environment, directory = tls_environment(tmp_path, monkeypatch)
    if http_local:
        chatwoot_compose.HTTP_LOCAL_DROPIN.touch()
    monkeypatch.setattr(chatwoot_compose, "ENV_FILE", environment)
    monkeypatch.setenv("CHATWOOT_INTERNAL_TLS", "0")
    monkeypatch.setenv("INTERNAL_TLS_DIR", "/wrong-directory")
    monkeypatch.setenv("POSTGRES_TLS_CA_FILE", "/wrong-ca")
    calls = []
    monkeypatch.setattr(
        chatwoot_compose.subprocess,
        "call",
        lambda args, **kwargs: calls.append((args, kwargs)) or 0,
    )
    for args in (["up", "-d", "chatwoot"], ["stop", "agent-bot"], ["stop"]):
        monkeypatch.setattr(chatwoot_compose.sys, "argv", ["chatwoot_compose.py", *args])
        assert chatwoot_compose.main() == 0
        command, options = calls[-1]
        assert command[:2] == ["docker", "compose"]
        files = [command[index + 1] for index, value in enumerate(command) if value == "-f"]
        assert files[:3] == [
            "deploy/chatwoot/compose.yml",
            "deploy/chatwoot/compose.managed.yml",
            "deploy/timeweb/compose.yml",
        ]
        assert "deploy/chatwoot/compose.tls.yml" in files
        assert "deploy/chatwoot/compose.tls-postgres.yml" not in files
        assert ("deploy/timeweb/compose.http-local.yml" in files) == http_local
        assert (files[-1] == "deploy/timeweb/compose.tls-http-local.yml") == http_local
        assert options["env"]["INTERNAL_TLS_DIR"] == str(directory)
        assert command[-len(args) :] == args
        assert "synthetic-value-that-must-not-be-logged" not in str(command)
        assert "NEVIDIMY_AGENT_WEBHOOK_URL" not in options["env"]


@pytest.mark.parametrize(
    "fault",
    ["missing-key", "public-key", "missing-flag", "invalid-flag", "internal-db-ca", "http-webhook"],
)
def test_invalid_tls_setup_refuses_to_invoke_compose(tmp_path, monkeypatch, capsys, fault):
    environment, directory = tls_environment(tmp_path, monkeypatch)
    if fault == "missing-key":
        (directory / "redis.key").unlink()
    elif fault == "public-key":
        (directory / "chatwoot.key").chmod(0o644)
    elif fault == "internal-db-ca":
        environment.write_text(
            environment.read_text().replace(
                str(chatwoot_compose.POSTGRES_CA), str(directory / "ca.crt")
            )
        )
    elif fault == "http-webhook":
        environment.write_text(
            environment.read_text().replace("https://agent-bot", "http://agent-bot")
        )
    else:
        environment.write_text(
            environment.read_text().replace(
                "CHATWOOT_INTERNAL_TLS=1",
                "" if fault == "missing-flag" else "CHATWOOT_INTERNAL_TLS=yes",
            )
        )
    monkeypatch.setattr(chatwoot_compose, "ENV_FILE", environment)
    monkeypatch.setattr(
        chatwoot_compose.subprocess,
        "call",
        lambda *args, **kwargs: pytest.fail("must not start Compose"),
    )
    assert chatwoot_compose.main() == 2
    assert "synthetic-value" not in capsys.readouterr().err


def test_existing_timeweb_http_mode_is_preserved_until_explicit_tls_activation(tmp_path):
    environment = tmp_path / "platform.env"
    environment.write_text("CHATWOOT_HOSTNAME=example.test\n")
    files, _ = chatwoot_compose.compose_files(environment, http_local=True)
    assert files == [
        "deploy/chatwoot/compose.yml",
        "deploy/chatwoot/compose.managed.yml",
        "deploy/timeweb/compose.yml",
        "deploy/timeweb/compose.http-local.yml",
    ]

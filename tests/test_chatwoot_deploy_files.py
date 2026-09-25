from pathlib import Path

from scripts.import_chatwoot_certificate_pdfs import _certificate_paths

ROOT = Path(__file__).parent.parent


def test_test_stack_uses_latest_chatwoot_and_keeps_postgres_local() -> None:
    compose = (ROOT / "deploy/chatwoot/compose.yml").read_text(encoding="utf-8")

    assert compose.count("image: docker.io/chatwoot/chatwoot:latest") == 2
    assert "pgvector/pgvector:0.8.6-pg18-trixie" in compose
    assert "redis:7.4.10-alpine3.21" in compose


def test_agent_image_copies_only_tracked_certificate_import_modules() -> None:
    containerfile = (ROOT / "deploy/chatwoot/Containerfile").read_text(encoding="utf-8")

    assert "scripts/import_chatwoot_certificate_pdfs.py" in containerfile
    assert "scripts/certificate_pdf_runtime.py" in containerfile
    assert "scripts/reset_test_certificates.py" in containerfile
    assert "scripts/purge_test_certificates.py" in containerfile
    assert "scripts/__init__.py" not in containerfile


def test_certificate_import_ignores_macos_metadata_files(tmp_path: Path) -> None:
    certificate = tmp_path / "certificate.pdf"
    certificate.touch()
    (tmp_path / "._certificate.pdf").touch()

    assert _certificate_paths(tmp_path) == [certificate]


def test_certificate_identity_key_is_kept_in_root_only_agent_environment() -> None:
    deploy_script = (ROOT / "scripts/deploy_chatwoot_test.sh").read_text(encoding="utf-8")
    assert "CERTIFICATE_IDENTITY_KEY" in deploy_script
    assert "openssl rand -hex 32" in deploy_script
    assert 'sudo chmod 0600 "$agent_env_tmp"' in deploy_script


def test_agent_bot_uses_chatwoot_database_without_a_persistent_volume() -> None:
    compose = (ROOT / "deploy/chatwoot/compose.yml").read_text(encoding="utf-8")
    agent_block = compose.split("  agent-bot:\n", 1)[1].split("\n  caddy:", 1)[0]

    assert "DATABASE_URL" not in agent_block
    assert "volumes:" not in agent_block
    assert "depends_on:" not in agent_block


def test_caddy_exposes_chatwoot_and_agent_over_separate_hostnames() -> None:
    caddyfile = (ROOT / "deploy/chatwoot/Caddyfile").read_text(encoding="utf-8")

    assert "{$CHATWOOT_HOSTNAME}" in caddyfile
    assert "{$AGENT_HOSTNAME}" in caddyfile
    assert "agent-bot:8080" in caddyfile


def test_agent_webhook_has_a_separate_secret_url_path() -> None:
    app = (ROOT / "app/chatwoot/app.py").read_text(encoding="utf-8")
    deploy_script = (ROOT / "scripts/deploy_chatwoot_test.sh").read_text(encoding="utf-8")

    assert "CHATWOOT_WEBHOOK_SECRET" in deploy_script
    assert "CHATWOOT_WEBHOOK_HMAC_SECRET" in deploy_script
    assert '"/webhooks/chatwoot/agent/{route_secret}"' in app


def test_compose_does_not_interpolate_secret_values_into_process_arguments() -> None:
    compose = (ROOT / "deploy/chatwoot/compose.yml").read_text(encoding="utf-8")
    deploy_script = (ROOT / "scripts/deploy_chatwoot_test.sh").read_text(encoding="utf-8")

    # podman-compose 1.x logs generated `podman run` commands. Keep secrets in
    # root-only env files so they never become values in those commands.
    assert "${POSTGRES_PASSWORD}" not in compose
    assert "${SECRET_KEY_BASE}" not in compose
    assert "${CHATWOOT_WEBHOOK_SECRET}" not in compose
    assert "${CHATWOOT_WEBHOOK_HMAC_SECRET}" not in compose
    assert "run --rm --no-deps -T chatwoot" in deploy_script
    assert "bundle exec rails db:chatwoot_prepare </dev/null" in deploy_script
    assert 'sudo /usr/bin/tee -a "$agent_env_tmp" >/dev/null' in deploy_script


def test_test_stack_has_an_explicit_operator_bootstrap_and_persistent_services() -> None:
    env_example = (ROOT / "deploy/chatwoot/.env.example").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/chatwoot/compose.yml").read_text(encoding="utf-8")
    platform_unit = (ROOT / "deploy/chatwoot/women-help-chatwoot.service").read_text(
        encoding="utf-8"
    )
    agent_unit = (ROOT / "deploy/chatwoot/women-help-chatwoot-agent.service").read_text(
        encoding="utf-8"
    )
    deploy_script = (ROOT / "scripts/deploy_chatwoot_test.sh").read_text(encoding="utf-8")

    assert "ENABLE_ACCOUNT_SIGNUP=true" in env_example
    assert "profiles:" in compose
    assert "agent" in compose
    assert "podman compose" in platform_unit
    assert "up -d agent-bot" in agent_unit
    assert "--profile" not in agent_unit
    assert "WorkingDirectory=/opt/women-help-chatwoot\n" in platform_unit
    assert "WorkingDirectory=/opt/women-help-chatwoot\n" in agent_unit
    assert "up -d postgres redis telegram-proxy chatwoot sidekiq caddy" in platform_unit
    assert "git archive" in deploy_script
    assert "women-help-chatwoot.service" in deploy_script
    assert "systemctl restart women-help-chatwoot.service" in deploy_script


def test_justfile_and_readme_expose_chatwoot_operator_commands() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "deploy-chatwoot-test" in justfile
    assert "chatwoot-check" in justfile
    assert "chatwoot-bootstrap" in justfile
    assert "bootstrap_chatwoot_agent.sh" in justfile
    assert "certificates-import-pdf" in justfile
    assert "certificates-reset-test" in justfile
    assert "certificates-purge-test" in justfile
    assert "Chatwoot" in readme
    assert "women-help-chatwoot-agent" in readme


def test_polling_activation_has_one_owner_for_container_start() -> None:
    unit = (ROOT / "deploy/chatwoot/women-help-telegram-ingress.service").read_text()
    activate = (ROOT / "scripts/enable_chatwoot_polling.sh").read_text()
    assert "up -d --force-recreate --no-deps telegram-ingress" in unit
    assert "systemctl enable --now women-help-telegram-ingress.service" in activate
    assert "up -d" not in activate

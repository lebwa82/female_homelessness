import json
from pathlib import Path

import pytest

from analytics.dds.__main__ import main, read_identity_key, validate_dsn


def test_remote_database_requires_full_tls_validation():
    with pytest.raises(ValueError):
        validate_dsn("postgresql://u:DO_NOT_PRINT@remote.invalid/db?sslmode=require")
    validate_dsn("postgresql://u@remote.invalid/db?sslmode=verify-full")
    validate_dsn("postgresql://u@127.0.0.1:55487/db")


def test_catalog_validation_does_not_need_credentials(capsys):
    assert main(["validate"]) == 0
    assert json.loads(capsys.readouterr().out)["requirements"] == 105


def test_mutation_requires_explicit_apply(capsys):
    assert main(["load"]) == 2
    assert "apply" in capsys.readouterr().out


def test_identity_key_permissions_and_length(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"x" * 32)
    key.chmod(0o644)
    with pytest.raises(ValueError):
        read_identity_key(key)
    key.chmod(0o600)
    assert read_identity_key(key) == b"x" * 32
    key.write_bytes(b"short")
    with pytest.raises(ValueError):
        read_identity_key(key)


def test_cli_does_not_import_bot_settings():
    code = Path("analytics/dds/__main__.py").read_text()
    assert "app.config" not in code
    assert "app.chatwoot" not in code


def test_configuration_error_never_echoes_values(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text('{"account_id": "DO_NOT_PRINT"}')
    assert main(["--config", str(config), "load", "--apply"]) == 2
    assert "DO_NOT_PRINT" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@localhost:5432,remote.invalid:5432/db",
        "postgresql://u@127.0.0.1%2Cremote.invalid/db",
        "postgresql://u@127.0.0.1/db?host=remote.invalid",
        "postgresql://u@127.0.0.1/db?sslmode=disable&sslmode=verify-full",
        "postgresql://u@127.0.0.1/db?options=-c%20statement_timeout=0",
    ],
)
def test_endpoint_overrides_and_multihost_dsns_are_rejected(dsn):
    with pytest.raises(ValueError):
        validate_dsn(dsn)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@[::1],remote.invalid:5432/db",
        "postgresql://u@[::1]junk/db",
        "postgresql://u@[::1]:5432,remote.invalid:5432/db",
    ],
)
def test_ipv6_authority_must_be_one_complete_endpoint(dsn):
    with pytest.raises(ValueError):
        validate_dsn(dsn)

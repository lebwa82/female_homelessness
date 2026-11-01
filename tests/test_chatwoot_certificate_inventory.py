from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.chatwoot import certificates
from app.chatwoot.certificates import CertificateInventory
from app.config import Settings


def test_certificate_database_url_preserves_the_local_stack_defaults(monkeypatch) -> None:
    for name in (
        "CERTIFICATE_DATABASE_HOST", "CERTIFICATE_DATABASE_PORT", "CERTIFICATE_DATABASE_USER",
        "CERTIFICATE_DATABASE_NAME", "CERTIFICATE_DATABASE_SSLMODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(certificates, "settings", Settings(_env_file=None))

    assert certificates.database_url("test-password") == (
        "postgresql+asyncpg://chatwoot:test-password@postgres:5432/chatwoot"
    )


def test_certificate_database_url_supports_managed_postgres_and_tls(monkeypatch) -> None:
    configured = Settings(
        _env_file=None,
        CERTIFICATE_DATABASE_HOST="database.example.test",
        CERTIFICATE_DATABASE_PORT=6543,
        CERTIFICATE_DATABASE_USER="cloud_user",
        CERTIFICATE_DATABASE_NAME="default_db",
        CERTIFICATE_DATABASE_SSLMODE="verify-full",
    )
    monkeypatch.setattr(certificates, "settings", configured)
    password = "test-only:p@ss/word?#%"

    url = make_url(certificates.database_url(password))

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "database.example.test"
    assert url.port == 6543
    assert url.username == "cloud_user"
    assert url.database == "default_db"
    assert url.password == password
    assert url.query == {"ssl": "verify-full"}


@pytest.mark.parametrize("port", [0, 65536])
def test_certificate_database_rejects_invalid_ports(port) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, CERTIFICATE_DATABASE_PORT=port)


def test_certificate_database_rejects_unknown_tls_modes() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, CERTIFICATE_DATABASE_SSLMODE="unrecognized")


async def test_recipient_hash_is_stable_and_scoped_to_chatwoot_account() -> None:
    url = "postgresql+asyncpg://test:test@localhost/test"
    first = CertificateInventory(url, identity_key="test-secret", account_id=1)
    second = CertificateInventory(url, identity_key="test-secret", account_id=1)
    other_account = CertificateInventory(url, identity_key="test-secret", account_id=2)
    try:
        assert first._recipient_hash(7) == second._recipient_hash(7)
        assert first._recipient_hash(7) != first._recipient_hash(8)
        assert first._recipient_hash(7) != other_account._recipient_hash(7)
        assert len(first._recipient_hash(7)) == 64
    finally:
        await first.close()
        await second.close()
        await other_account.close()


@pytest.mark.parametrize("aids", [(), ("food_card",), ("food_card", "children_card", "food_card")])
@pytest.mark.parametrize("allow_test", [None, False, True])
async def test_available_aid_ids_is_a_global_read_only_snapshot(aids, allow_test, monkeypatch) -> None:
    connection = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = aids
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(certificates, "create_async_engine", lambda *args, **kwargs: engine)
    options = {} if allow_test is None else {"allow_test": allow_test}
    inventory = CertificateInventory("synthetic", identity_key="test", account_id=1, **options)

    available = await inventory.available_aid_ids()

    assert available == frozenset(aids)
    assert isinstance(available, frozenset)
    inventory._engine.begin.assert_not_called()
    connection.execute.assert_awaited_once()
    args, kwargs = connection.execute.call_args
    assert len(args) == 2 and not kwargs
    assert args[1] == {"allow_test": bool(allow_test)}
    query = " ".join(str(args[0]).split())
    assert query.startswith("SELECT DISTINCT a.aid_id ")
    assert "recipient_hash" not in query
    assert "FOR UPDATE" not in query

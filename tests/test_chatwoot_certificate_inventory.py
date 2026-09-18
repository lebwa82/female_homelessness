from app.chatwoot.certificates import CertificateInventory


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

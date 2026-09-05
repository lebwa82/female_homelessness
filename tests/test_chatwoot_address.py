from scripts.update_chatwoot_address import update_addresses


def test_address_refresh_preserves_credentials_and_is_idempotent(tmp_path):
    platform = tmp_path / "platform.env"
    agent = tmp_path / "agent.env"
    unchanged = '# keep this comment\nSECRET_KEY_BASE="fixture=value"\nENABLE_ACCOUNT_SIGNUP=false\n'
    platform.write_text(unchanged + 'CHATWOOT_HOSTNAME=chatwoot.192-0-2-1.sslip.io\n'
                        'AGENT_HOSTNAME=agent.192-0-2-1.sslip.io\n')
    agent.write_text('YANDEX_AI_API_KEY=fixture\nCHATWOOT_BASE_URL=https://chatwoot.192-0-2-1.sslip.io/\n')
    assert update_addresses("192.0.2.2", platform, agent) == (True, "https://chatwoot.192-0-2-2.sslip.io/")
    assert platform.read_text().startswith(unchanged)
    assert "AGENT_HOSTNAME=agent.192-0-2-2.sslip.io\n" in platform.read_text()
    assert agent.read_text() == ('YANDEX_AI_API_KEY=fixture\n'
                                'CHATWOOT_BASE_URL=https://chatwoot.192-0-2-2.sslip.io\n')
    before = platform.stat().st_mtime_ns
    assert update_addresses("192.0.2.2", platform, agent)[0] is False
    assert platform.stat().st_mtime_ns == before
    assert platform.stat().st_mode & 0o777 == 0o600


def test_address_refresh_preserves_custom_domains_and_internal_urls(tmp_path):
    platform = tmp_path / "platform.env"
    agent = tmp_path / "agent.env"
    original = 'CHATWOOT_HOSTNAME=support.example.org\nAGENT_HOSTNAME=agent.example.org\n'
    platform.write_text(original)
    agent.write_text('CHATWOOT_BASE_URL=http://chatwoot:3000\n')
    assert update_addresses("192.0.2.3", platform, agent) == (False, "https://support.example.org/")
    assert platform.read_text() == original
    assert agent.read_text() == 'CHATWOOT_BASE_URL=http://chatwoot:3000\n'

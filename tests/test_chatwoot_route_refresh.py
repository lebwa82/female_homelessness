from pathlib import Path

import pytest

from deploy.chatwoot import activate


def test_refresh_routes_uses_current_hosts_and_does_not_log_credentials(monkeypatch, capsys):
    values = {
        "CHATWOOT_HOSTNAME": "chatwoot.192-0-2-2.sslip.io",
        "AGENT_HOSTNAME": "agent.192-0-2-2.sslip.io",
        "CHATWOOT_WEBHOOK_SECRET": "private-fixture-route",
    }
    monkeypatch.setattr(activate, "env", lambda path: values)
    calls = []

    def rails(script, config):
        calls.append((script, config))
        return {"agent_route_updated": True, "verified_telegram_channels": [1]}

    monkeypatch.setattr(activate, "rails", rails)
    activate.refresh_routes()
    script, config = calls[0]
    assert config["frontend_url"] == "https://chatwoot.192-0-2-2.sslip.io"
    assert config["agent_health_url"] == "https://agent.192-0-2-2.sslip.io/healthz"
    assert config["agent_url"].startswith("https://agent.192-0-2-2.sslip.io/")
    assert "drop_pending_updates: false" in script
    assert "deleteWebhook" not in script
    assert "private-fixture-route" not in capsys.readouterr().out


def test_refresh_routes_does_not_report_success_after_failure(monkeypatch, capsys):
    monkeypatch.setattr(activate, "env", lambda path: {
        "CHATWOOT_HOSTNAME": "chatwoot.example.org",
        "AGENT_HOSTNAME": "agent.example.org",
        "CHATWOOT_WEBHOOK_SECRET": "fixture",
    })

    def fail(*args):
        raise RuntimeError("not ready")

    monkeypatch.setattr(activate, "rails", fail)
    with pytest.raises(RuntimeError):
        activate.refresh_routes()
    assert capsys.readouterr().out == ""


def test_shell_refresh_checks_routes_even_if_env_hosts_did_not_change():
    script = Path("scripts/refresh_chatwoot_address.sh").read_text()
    assert "'sudo python3 - refresh_routes' < deploy/chatwoot/activate.py" in script
    assert script.index("ready=0") > script.index('if [[ "$changed" == "changed" ]]')
    assert script.index("refresh_routes") > script.index("HTTPS check failed")

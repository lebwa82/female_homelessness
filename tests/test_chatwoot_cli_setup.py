import subprocess

import pytest

from scripts import setup_chatwoot_cli as setup


def test_login_uses_stdin_and_does_not_print_credentials(monkeypatch, capsys):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "internal output", "")

    monkeypatch.setattr(setup.subprocess, "run", run)
    setup.login("chatwoot", "lebwa82@192.0.2.1", {"token": "test-only", "account_id": 1})
    args, kwargs = calls[0]
    assert "test-only" not in str(args)
    assert kwargs["input"] == "https://chatwoot.192-0-2-1.sslip.io\ntest-only\n1\n"
    assert kwargs["capture_output"]
    assert "test-only" not in capsys.readouterr().out


@pytest.mark.parametrize("token,account", [("", 1), ("a\nb", 1), ("a\rb", 1), ("test", 2)])
def test_invalid_credentials_do_not_launch_cli(monkeypatch, token, account):
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("unexpected CLI"))
    with pytest.raises(ValueError):
        setup.login("chatwoot", "lebwa82@192.0.2.1", {"token": token, "account_id": account})


def test_login_failure_does_not_surface_subprocess_output(monkeypatch):
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "test-only", "test-only"))
    with pytest.raises(RuntimeError, match="suppressed") as error:
        setup.login("chatwoot", "lebwa82@192.0.2.1", {"token": "test-only", "account_id": 1})
    assert "test-only" not in str(error.value)


def test_host_verification_precedes_secret_transfer(monkeypatch):
    monkeypatch.setattr(setup, "cli_binary", lambda: "chatwoot")
    monkeypatch.setattr(setup, "resolve_prod_host", lambda: "lebwa82@192.0.2.1")

    def reject(_):
        raise ValueError("wrong VM")

    monkeypatch.setattr(setup, "verify_ssh_host", reject)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("unexpected SSH"))
    with pytest.raises(ValueError, match="wrong VM"):
        setup.main()

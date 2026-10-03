from contextlib import nullcontext
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from app.chatwoot import healthcheck


@pytest.mark.parametrize("status, expected", [(200, True), (503, False)])
def test_health_status(monkeypatch, status, expected):
    def open_probe(url, timeout):
        assert url == "http://127.0.0.1:8081/healthz"
        assert timeout == 4
        return nullcontext(SimpleNamespace(status=status))

    monkeypatch.setattr(healthcheck, "urlopen", open_probe)
    assert healthcheck.is_healthy(8081) is expected


@pytest.mark.parametrize("error", [URLError("unavailable"), TimeoutError(), ValueError()])
def test_health_failure_is_silent(monkeypatch, capsys, error):
    def open_probe(*args, **kwargs):
        raise error

    monkeypatch.setattr(healthcheck, "urlopen", open_probe)
    assert not healthcheck.is_healthy(8080)
    assert capsys.readouterr() == ("", "")

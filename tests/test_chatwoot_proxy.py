import pytest

from app.chatwoot.proxy import upstream_uri


def test_socks_credentials_are_converted_to_library_uri_format():
    assert upstream_uri("socks5://user:pass%40word@proxy.test:1080") == (
        "socks5://proxy.test:1080#user:pass@word"
    )


def test_https_proxy_uses_library_tls_scheme():
    assert upstream_uri("https://proxy.test:443") == "http+ssl://proxy.test:443"


def test_missing_port_raises_without_echoing_secret():
    with pytest.raises(ValueError, match="Unsupported Telegram proxy configuration"):
        upstream_uri("socks5://user:password@proxy.test")

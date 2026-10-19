"""TLS contexts for optional internal HTTPS; certificate errors never fall back."""

from __future__ import annotations

import ssl


def server_context(cert_file: str, key_file: str) -> ssl.SSLContext | None:
    if not cert_file and not key_file:
        return None
    if not cert_file or not key_file:
        raise ValueError("Internal HTTPS requires both certificate and private key")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context

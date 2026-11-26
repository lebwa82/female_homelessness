"""Verify native Chatwoot webhook signatures for bots and API inboxes."""

from __future__ import annotations

import hashlib
import hmac
from time import time

MAX_WEBHOOK_AGE_SECONDS = 300


class InvalidWebhookSignature(ValueError):
    """Uniform failure for missing, invalid, or replayed webhook requests."""


def verify_webhook_signature(
    *,
    raw_body: bytes,
    timestamp: str,
    received_signature: str,
    secret: str,
    now: float | None = None,
    max_age_seconds: int = MAX_WEBHOOK_AGE_SECONDS,
) -> None:
    """Verify Chatwoot's HMAC over the untouched request body.

    Chatwoot signs ``{timestamp}.{raw_body}`` with HMAC-SHA256 and prefixes
    the hexadecimal digest with ``sha256=``.  Timestamp validation prevents
    a valid historical delivery from being replayed indefinitely.
    """
    if not (isinstance(timestamp, str) and 1 <= len(timestamp) <= 16
            and timestamp.isascii() and timestamp.isdecimal()
            and isinstance(received_signature, str) and received_signature.isascii()):
        raise InvalidWebhookSignature("invalid webhook")
    sent_at = int(timestamp)
    reference_time = time() if now is None else now
    if abs(reference_time - sent_at) > max_age_seconds:
        raise InvalidWebhookSignature("invalid webhook")

    signed = timestamp.encode("ascii") + b"." + raw_body
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_signature):
        raise InvalidWebhookSignature("invalid webhook")

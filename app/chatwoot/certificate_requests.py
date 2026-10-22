"""Persistent certificate demand, independent of the navigable conversation state."""

from __future__ import annotations

import json
import re

from sqlalchemy import text

AID_POOLS = {
    "food_card": "pyaterochka", "children_card": "pyaterochka",
    "medicine_card": "ozon", "hostel_3_nights": "ozon",
}


async def initialize_schema(connection) -> None:
    """Initialize after the inventory tables, in the caller's transaction."""
    await connection.execute(text("""
        CREATE TABLE IF NOT EXISTS women_help_certificate_requests (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            account_id BIGINT NOT NULL CHECK (account_id > 0),
            recipient_hash VARCHAR(64) NOT NULL CHECK (recipient_hash ~ '^[0-9a-f]{64}$'),
            contact_id BIGINT NOT NULL CHECK (contact_id > 0),
            conversation_id BIGINT NOT NULL CHECK (conversation_id > 0),
            source_message_id BIGINT NOT NULL CHECK (source_message_id > 0),
            last_conversation_id BIGINT NOT NULL CHECK (last_conversation_id > 0),
            last_source_message_id BIGINT NOT NULL CHECK (last_source_message_id > 0),
            aid_id VARCHAR(64) NOT NULL,
            pool_id BIGINT NOT NULL REFERENCES women_help_certificate_pools(id),
            reason VARCHAR(16) NOT NULL CHECK (reason IN ('paused','out_of_stock')),
            status VARCHAR(16) NOT NULL DEFAULT 'waiting'
                CHECK (status IN ('waiting','fulfilled','cancelled')),
            first_requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            source_event_keys JSONB NOT NULL CHECK (
                jsonb_typeof(source_event_keys) = 'array'
                AND jsonb_array_length(source_event_keys) > 0
            ),
            fulfilled_certificate_id BIGINT REFERENCES women_help_pdf_certificates(id)
                ON DELETE SET NULL,
            CHECK (fulfilled_certificate_id IS NULL OR status = 'fulfilled')
        )
    """))
    await connection.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_women_help_certificate_requests_waiting
        ON women_help_certificate_requests (recipient_hash, pool_id)
        WHERE status = 'waiting'
    """))
    await connection.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_women_help_certificate_requests_queue
        ON women_help_certificate_requests (pool_id, first_requested_at, id)
        WHERE status = 'waiting'
    """))
    await connection.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_women_help_certificate_requests_recipient
        ON women_help_certificate_requests (recipient_hash)
    """))


async def record_request(
    connection, *, account_id: int, recipient_hash: str, contact_id: int,
    aid_id: str, request_key: str, conversation_id: int, source_message_id: int,
    reason: str,
) -> bool:
    """Record demand; the caller holds the inventory recipient advisory lock.

    The caller supplies the same account/contact HMAC used by inventory and
    owns the transaction. False means replay or an already submitted/delivered
    certificate. Recording demand never reserves inventory.
    """
    if reason not in {"paused", "out_of_stock"}:
        raise ValueError("invalid certificate request reason")
    if not isinstance(request_key, str) or not request_key or len(request_key) > 128:
        raise ValueError("invalid certificate request key")
    if aid_id not in AID_POOLS:
        raise ValueError("unknown certificate aid")
    if not isinstance(recipient_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", recipient_hash):
        raise ValueError("invalid certificate recipient hash")
    if any(type(value) is not int or value <= 0 for value in (
        account_id, contact_id, conversation_id, source_message_id,
    )):
        raise ValueError("certificate request IDs must be positive integers")
    params = {
        "account_id": account_id, "recipient_hash": recipient_hash,
        "contact_id": contact_id, "aid_id": aid_id, "request_key": request_key,
        "conversation_id": conversation_id, "source_message_id": source_message_id,
        "reason": reason, "pool_slug": AID_POOLS[aid_id],
        "source_event_keys": json.dumps([request_key]),
    }
    replay = (await connection.execute(text("""
        SELECT id FROM women_help_certificate_requests
        WHERE recipient_hash = :recipient_hash AND jsonb_exists(source_event_keys, :request_key)
        LIMIT 1
    """), params)).scalar_one_or_none()
    if replay is not None:
        return False
    issued = (await connection.execute(text("""
        SELECT id FROM women_help_pdf_certificates
        WHERE recipient_hash = :recipient_hash
          AND (record_kind = 'historical_issuance' OR status IN ('submitted','delivered')
               OR submitted_at IS NOT NULL OR delivered_at IS NOT NULL)
        LIMIT 1
    """), params)).scalar_one_or_none()
    if issued is not None:
        return False
    pool_id = (await connection.execute(text("""
        SELECT id FROM women_help_certificate_pools WHERE slug = :pool_slug
    """), params)).scalar_one_or_none()
    if pool_id is None:
        raise ValueError("certificate pool is not initialized")
    params["pool_id"] = pool_id
    await connection.execute(text("""
        INSERT INTO women_help_certificate_requests (
            account_id, recipient_hash, contact_id, conversation_id, source_message_id,
            last_conversation_id, last_source_message_id, aid_id, pool_id, reason,
            source_event_keys
        ) VALUES (
            :account_id, :recipient_hash, :contact_id, :conversation_id, :source_message_id,
            :conversation_id, :source_message_id, :aid_id, :pool_id, :reason,
            CAST(:source_event_keys AS jsonb)
        )
        ON CONFLICT (recipient_hash, pool_id) WHERE status = 'waiting'
        DO UPDATE SET last_requested_at = now(), aid_id = EXCLUDED.aid_id,
            last_conversation_id = EXCLUDED.last_conversation_id,
            last_source_message_id = EXCLUDED.last_source_message_id,
            reason = EXCLUDED.reason,
            source_event_keys = women_help_certificate_requests.source_event_keys
                || EXCLUDED.source_event_keys
    """), params)
    return True

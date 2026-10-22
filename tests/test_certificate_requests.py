"""Deterministic database call contracts for durable certificate requests."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.chatwoot.certificate_requests import initialize_schema, record_request

PARAMS = {
    "account_id": 1, "recipient_hash": "a" * 64, "contact_id": 7,
    "conversation_id": 23, "source_message_id": 41, "request_key": "event:first",
    "aid_id": "food_card", "reason": "paused",
}


def connection(*results):
    return SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda value=value: value)
        for value in results
    ]))


async def test_new_request_keeps_source_key_and_paused_reason_without_reserving_inventory():
    db = connection(None, None, 4, None)
    assert await record_request(db, **PARAMS)
    assert db.execute.await_count == 4
    query, values = db.execute.await_args.args
    assert "INSERT INTO women_help_certificate_requests" in str(query)
    assert values["reason"] == "paused"
    assert values["pool_slug"] == "pyaterochka"
    assert json.loads(values["source_event_keys"]) == [PARAMS["request_key"]]
    assert values["source_message_id"] == 41
    assert all("UPDATE women_help_pdf_certificates" not in str(call.args[0])
               for call in db.execute.await_args_list)


async def test_any_recorded_source_key_is_replay_even_after_later_events_or_cancellation():
    db = connection(17)
    assert not await record_request(db, **PARAMS)
    db.execute.assert_awaited_once()
    query = str(db.execute.await_args.args[0])
    assert "jsonb_exists(source_event_keys, :request_key)" in query
    assert "status =" not in query
    assert "last_source_message_id" not in query


async def test_existing_or_historical_issuance_does_not_create_waiting_request():
    db = connection(None, 5)
    assert not await record_request(db, **PARAMS)
    assert db.execute.await_count == 2
    query = str(db.execute.await_args.args[0])
    assert "record_kind = 'historical_issuance'" in query
    assert "submitted_at IS NOT NULL" in query
    assert "delivered_at IS NOT NULL" in query


async def test_request_uses_pool_shared_by_related_aids_and_preserves_original_queue_position():
    db = connection(None, None, 4, None)
    assert await record_request(db, **{**PARAMS, "aid_id": "children_card"})
    query, values = db.execute.await_args.args
    assert values["pool_slug"] == "pyaterochka"
    update = str(query).split("DO UPDATE SET", 1)[1]
    assert "first_requested_at" not in update
    assert "last_requested_at = now()" in update
    assert "|| EXCLUDED.source_event_keys" in update


@pytest.mark.parametrize("changed", (
    {"reason": "unavailable"}, {"aid_id": "legal_consultation"},
    {"recipient_hash": "malformed"}, {"contact_id": 0},
    {"conversation_id": -1}, {"account_id": True}, {"source_message_id": "41"},
    {"request_key": ""}, {"request_key": "a" * 129},
))
async def test_invalid_request_metadata_fails_before_any_database_call(changed):
    db = connection()
    with pytest.raises(ValueError):
        await record_request(db, **{**PARAMS, **changed})
    db.execute.assert_not_awaited()


async def test_schema_has_one_active_request_per_recipient_and_pool_and_ordered_queue():
    db = SimpleNamespace(execute=AsyncMock())
    await initialize_schema(db)
    sql = "\n".join(str(call.args[0]) for call in db.execute.await_args_list)
    assert "(recipient_hash, pool_id)" in sql
    assert "WHERE status = 'waiting'" in sql
    assert "(pool_id, first_requested_at, id)" in sql
    assert "REFERENCES women_help_pdf_certificates(id)" in sql
    assert "'paused','out_of_stock'" in sql
    assert "'waiting','fulfilled','cancelled'" in sql

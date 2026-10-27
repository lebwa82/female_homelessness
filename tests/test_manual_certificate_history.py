"""Synthetic deterministic checks; no configured database or dialogue fixtures."""

import copy
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.chatwoot.manual_certificate_history import (
    ManualHistoricalIssuance,
    import_manual_history,
    initialize_manual_schema,
    verify_manual_history,
)


def record(**changes):
    return ManualHistoricalIssuance(**{
        "account_id": 1, "contact_id": 7, "conversation_id": 11,
        "source_message_id": 13, "issued_on": date(2026, 10, 1),
        "accepted_at": datetime(2026, 10, 3, 12, tzinfo=UTC),
        "provider_slug": "ozon", "aid_id": "children_card",
        "source_reference": "synthetic-sheet:gid=0:row=2",
        "evidence": {"kind": "user_attested_sheet", "row": 2}, **changes,
    })


class Result:
    def __init__(self, rows=()):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return next(iter(self.rows), None)

    def all(self):
        return list(self.rows)


class Connection:
    def __init__(self):
        self.rows, self.queries, self.locks = [], [], []
        self.pools = {"ozon", "pyaterochka"}

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy(self.rows)
        try:
            yield self
        except Exception:
            self.rows = before
            raise

    async def execute(self, statement, params=None):
        query = " ".join(str(statement).split())
        self.queries.append(query)
        assert "women_help_certificate_requests" not in query
        if "pg_advisory_xact_lock" in query:
            self.locks.append(params["lock_key"])
            return Result()
        if query.startswith("SELECT c.record_kind"):
            return Result([
                row for row in self.rows
                if row["recipient_hash"] == params["recipient_hash"]
                or (row["account_id"] == params["account_id"]
                    and row["source_message_id"] == params["source_message_id"])
            ])
        if query.startswith("INSERT INTO women_help_pdf_certificates"):
            if params["provider_slug"] not in self.pools:
                return Result()
            self.rows.append({
                **params, "historical_evidence": json.loads(params["historical_evidence"]),
                "record_kind": "historical_issuance", "history_only": True,
            })
            return Result([{"id": len(self.rows)}])
        raise AssertionError("unexpected operation")


async def imported(connection, records):
    async with connection.transaction():
        return await import_manual_history(
            connection, records, identity_key="synthetic-key", account_id=1,
        )


async def test_repeatable_import_keeps_attested_date_separate_from_acceptance():
    connection = Connection()
    accepted, recorded = record(), record(contact_id=8, source_message_id=14, accepted_at=None)
    assert await imported(connection, [accepted, accepted, recorded]) == (2, 1)
    assert await imported(connection, [accepted, recorded]) == (0, 2)
    assert [row["status"] for row in connection.rows] == ["accepted", "recorded"]
    assert connection.rows[0]["accepted_at"].date() != accepted.issued_on
    assert connection.rows[1]["accepted_at"] is None
    assert connection.rows[0]["provider_slug"] == "ozon"
    assert connection.rows[0]["selected_aid_id"] == "children_card"
    assert await verify_manual_history(
        connection, [accepted, recorded], identity_key="synthetic-key", account_id=1,
    ) == 2
    # Read queries may test NULLness but never return bearer fields or c.*.
    for query in connection.queries:
        if query.startswith("SELECT c.record_kind"):
            assert "c.*" not in query
            assert "c.activation_code IS NULL" in query


async def test_recipient_hmac_and_sorted_locks_match_inventory():
    connection = Connection()
    records = [record(contact_id=value, source_message_id=100 + value) for value in [9, 7, 8]]
    await imported(connection, records)
    hashes = [hmac.new(b"synthetic-key", f"1:{row.contact_id}".encode(), hashlib.sha256)
              .hexdigest() for row in records]
    assert [row["recipient_hash"] for row in connection.rows] == hashes
    assert connection.locks == sorted(
        int.from_bytes(bytes.fromhex(value[:16]), "big", signed=True) for value in hashes
    )


@pytest.mark.parametrize("changes", [
    {"conversation_id": 12}, {"source_message_id": 14}, {"contact_id": 8},
    {"issued_on": date(2026, 10, 2)}, {"accepted_at": None},
    {"provider_slug": "pyaterochka"}, {"aid_id": None},
    {"source_reference": "synthetic-sheet:gid=0:row=3"},
    {"evidence": {"kind": "user_attested_sheet", "row": 3}},
])
async def test_metadata_changes_conflict_and_roll_back_the_whole_batch(changes):
    connection = Connection()
    await imported(connection, [record()])
    before = copy.deepcopy(connection.rows)
    with pytest.raises(ValueError, match="conflicts"):
        await imported(connection, [
            record(contact_id=9, source_message_id=99), record(**changes),
        ])
    assert connection.rows == before


@pytest.mark.parametrize("change", [
    {"record_kind": "certificate"}, {"history_only": False}, {"status": "delivered"},
])
async def test_real_or_modified_rows_are_never_overwritten(change):
    connection = Connection()
    await imported(connection, [record()])
    connection.rows[0].update(change)
    before = copy.deepcopy(connection.rows)
    with pytest.raises(ValueError, match="conflicts"):
        await imported(connection, [record()])
    with pytest.raises(ValueError, match="does not match"):
        await verify_manual_history(
            connection, [record()], identity_key="synthetic-key", account_id=1,
        )
    assert connection.rows == before


async def test_missing_pool_never_creates_a_null_pool_history_row():
    connection = Connection()
    connection.pools.clear()
    with pytest.raises(ValueError, match="pool is missing"):
        await imported(connection, [record()])
    assert connection.rows == []


@pytest.mark.parametrize("changes", [
    {"account_id": True}, {"contact_id": 0}, {"conversation_id": -1},
    {"source_message_id": None}, {"issued_on": "2026-10-01"},
    {"issued_on": datetime(2026, 10, 1, tzinfo=UTC)},
    {"accepted_at": datetime(2026, 10, 1, tzinfo=UTC).replace(tzinfo=None)},
    {"accepted_at": "2026-10-01"},
    {"provider_slug": None}, {"provider_slug": "other"},
    {"aid_id": "food_card"}, {"source_reference": " "},
    {"evidence": {}}, {"evidence": {"value": float("nan")}},
    {"evidence": {1: "value"}}, {"evidence": {"value": date(2026, 1, 1)}},
])
def test_record_rejects_unproven_or_invalid_metadata(changes):
    with pytest.raises(ValueError):
        record(**changes)


async def test_cross_account_and_missing_key_rejected_before_sql():
    connection = Connection()
    for records, key in [([record(account_id=2)], "synthetic-key"), ([record()], "")]:
        with pytest.raises(ValueError):
            await import_manual_history(connection, records, identity_key=key, account_id=1)
    assert connection.queries == []


async def test_mutated_evidence_revalidated_before_sql():
    connection, row = Connection(), record()
    row.evidence.clear()
    with pytest.raises(ValueError):
        await imported(connection, [row])
    assert connection.queries == []


async def test_schema_is_versioned_preserves_real_pdf_constraints_and_never_touches_requests():
    class SchemaConnection:
        def __init__(self):
            self.versions, self.queries = set(), []

        async def execute(self, statement, params=None):
            query = " ".join(str(statement).split())
            self.queries.append(query)
            assert "women_help_certificate_requests" not in query
            if query.startswith("SELECT 1"):
                return Result([{}] if params["version"] in self.versions else [])
            if query.startswith("INSERT INTO women_help_certificate_schema_migrations"):
                self.versions.add(params["version"])
            return Result()

    connection = SchemaConnection()
    await initialize_manual_schema(connection)
    assert connection.versions == {1, 2}
    assert "pg_advisory_xact_lock" in connection.queries[0]
    constraint = [query for query in connection.queries if "ADD CONSTRAINT" in query][-1]
    for field in ("pool_id", "activation_code", "serial_number", "expires_at", "pdf_bucket",
                  "pdf_object_key", "pdf_sha256", "pdf_size", "pdf_filename"):
        assert f"{field} IS NOT NULL" in constraint
    assert "status = 'recorded' AND accepted_at IS NULL" in constraint
    assert "issued_on IS NULL" in constraint and "historical_evidence IS NULL" in constraint
    before = len(connection.queries)
    await initialize_manual_schema(connection)
    assert not any("ALTER TABLE" in query for query in connection.queries[before:])


def test_historical_provider_exception_does_not_change_current_pool_allocation():
    from app.chatwoot.certificates import POOL_AIDS

    assert "children_card" not in POOL_AIDS["ozon"][1]
    assert replace(record(), aid_id="children_card").aid_id == "children_card"

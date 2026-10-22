"""Synthetic, deterministic historical-evidence regressions; no configured DB access."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.chatwoot.certificate_schema import initialize_schema
from app.chatwoot.certificates import CertificateInventory, HistoricalCertificateIssuance


def evidence(**overrides):
    return HistoricalCertificateIssuance(**{
        "account_id": 1, "contact_id": 7, "conversation_id": 11,
        "source_message_id": 13, "accepted_at": datetime(2025, 1, 2, tzinfo=UTC),
        **overrides,
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


class MemoryEngine:
    """Transaction rollback and identity behavior, with query traces for read-only checks."""

    def __init__(self):
        self.rows = []
        self.queries = []
        self.locks = []
        self.transactions = 0

    @asynccontextmanager
    async def begin(self):
        original = list(self.rows)
        self.transactions += 1
        try:
            yield self
        except Exception:
            self.rows = original
            raise

    @asynccontextmanager
    async def connect(self):
        yield self

    async def execute(self, statement, params=None):
        query = " ".join(str(statement).split())
        self.queries.append(query)
        if "pg_advisory_xact_lock" in query:
            self.locks.append(params["lock_key"])
            return Result()
        if "WHERE c.issuance_key" in query:
            return Result([
                row for row in self.rows
                if row.get("record_kind") == "certificate"
                and row.get("issuance_key") == params["issuance_key"]
                and row["recipient_hash"] == params["recipient_hash"]
            ])
        if query.startswith("SELECT c.*"):
            return Result([
                row for row in self.rows
                if row["recipient_hash"] == params["recipient_hash"]
                or ("source_message_id" in params
                    and row.get("account_id") == params["account_id"]
                    and row.get("source_message_id") == params["source_message_id"])
            ])
        if query.startswith("INSERT INTO women_help_pdf_certificates"):
            self.rows.append({**params, "record_kind": "historical_issuance", "status": "accepted"})
            return Result([{"id": len(self.rows)}])
        if query.startswith("UPDATE women_help_certificate_requests"):
            return Result([])
        raise AssertionError("unexpected inventory operation")


@pytest.fixture
def inventory():
    result = object.__new__(CertificateInventory)
    result._engine = MemoryEngine()
    result._identity_key, result._account_id = b"synthetic-test-key", 1
    return result


async def test_historical_import_is_exactly_repeatable_without_document_fields(inventory):
    record = evidence()
    assert await inventory.import_historical_issuances([record, record]) == (1, 1)
    assert await inventory.import_historical_issuances([record]) == (0, 1)
    assert len(inventory._engine.rows) == 1
    row = inventory._engine.rows[0]
    assert row["status"] == "accepted"
    assert row["provider_slug"] is None
    assert row["recipient_hash"] == inventory._recipient_hash(record.contact_id)
    assert not {"issuance_key", "pdf_object_key", "delivered_at"} & row.keys()
    expected_lock = int.from_bytes(
        bytes.fromhex(inventory._recipient_hash(7)[:16]), "big", signed=True,
    )
    assert inventory._engine.locks == [expected_lock, expected_lock]


async def test_historical_claim_is_already_issued_without_constructing_document(inventory):
    await inventory.import_historical_issuances([evidence()])
    result = await inventory.claim("food_card", "new-request", 7)
    assert result.status == "already_issued"
    assert result.certificate is None
    assert len(inventory._engine.rows) == 1


async def test_historical_resume_remains_exact_read_only_and_unavailable(inventory):
    await inventory.import_historical_issuances([evidence()])
    before = len(inventory._engine.queries)
    transactions = inventory._engine.transactions
    result = await inventory.resume("food_card", "new-request", 7)
    assert result.status == "unavailable"
    assert result.certificate is None
    assert inventory._engine.transactions == transactions
    assert len(inventory._engine.queries[before:]) == 1
    assert inventory._engine.queries[before].startswith("SELECT ")


@pytest.mark.parametrize("overrides", [
    {"conversation_id": 12}, {"source_message_id": 14},
    {"accepted_at": datetime(2025, 1, 3, tzinfo=UTC)},
    {"provider_slug": "ozon"}, {"aid_id": "food_card"},
    {"contact_id": 8},
])
async def test_changed_historical_identity_or_metadata_is_conflict(inventory, overrides):
    await inventory.import_historical_issuances([evidence()])
    original = list(inventory._engine.rows)
    with pytest.raises(ValueError, match="conflicts"):
        await inventory.import_historical_issuances([evidence(**overrides)])
    assert inventory._engine.rows == original


@pytest.mark.parametrize("status", ["reserved", "failed", "submitted", "delivered"])
async def test_real_document_history_is_never_replaced(inventory, status):
    original = {"recipient_hash": inventory._recipient_hash(7),
                "record_kind": "certificate", "status": status}
    inventory._engine.rows.append(original)
    with pytest.raises(ValueError, match="conflicts"):
        await inventory.import_historical_issuances([evidence()])
    assert inventory._engine.rows == [original]


async def test_conflict_rolls_back_entire_historical_batch(inventory):
    await inventory.import_historical_issuances([evidence()])
    original = list(inventory._engine.rows)
    with pytest.raises(ValueError, match="conflicts"):
        await inventory.import_historical_issuances([
            evidence(contact_id=8, source_message_id=14), evidence(conversation_id=12),
        ])
    assert inventory._engine.rows == original


async def test_cross_account_import_rejected_before_transaction(inventory):
    with pytest.raises(ValueError, match="another Chatwoot account"):
        await inventory.import_historical_issuances([evidence(account_id=2)])
    assert inventory._engine.transactions == 0


@pytest.mark.parametrize("overrides", [
    {"account_id": 0}, {"contact_id": -1}, {"conversation_id": True},
    {"source_message_id": "13"},
    {"accepted_at": datetime(2025, 1, 2, tzinfo=UTC).replace(tzinfo=None)},
    {"provider_slug": "unknown"}, {"aid_id": "unknown"},
    {"provider_slug": "ozon", "aid_id": "food_card"},
])
def test_historical_evidence_validates_identifiers_time_and_known_metadata(overrides):
    with pytest.raises(ValueError):
        evidence(**overrides)


async def test_schema_migration_is_locked_versioned_and_skips_applied_version():
    class Connection:
        def __init__(self, applied):
            self.applied = applied
            self.queries = []

        async def execute(self, statement, params=None):
            query = " ".join(str(statement).split())
            self.queries.append(query)
            return Result([{"version": 1}] if query.startswith("SELECT 1") and self.applied else [])

    pending = Connection(False)
    await initialize_schema(pending)
    assert "pg_advisory_xact_lock" in pending.queries[0]
    assert "ALTER TABLE" in " ".join(pending.queries)
    assert pending.queries[-1].startswith("INSERT INTO women_help_certificate_schema_migrations")
    checks = next(query for query in pending.queries if "ADD CONSTRAINT" in query)
    for field in (
        "pool_id", "nominal_rubles", "activation_code", "serial_number", "expires_at",
        "pdf_bucket", "pdf_object_key", "pdf_sha256", "pdf_size", "pdf_filename",
    ):
        assert f"{field} IS NOT NULL" in checks
    assert "status = 'accepted'" in checks
    assert "delivered_at IS NULL" in checks
    assert "issuance_key IS NULL" in checks
    applied = Connection(True)
    await initialize_schema(applied)
    assert len(applied.queries) == 3

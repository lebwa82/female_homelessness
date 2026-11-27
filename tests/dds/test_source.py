from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest

from analytics.dds import source

TABLES = {
    "contacts",
    "contact_inboxes",
    "inboxes",
    "conversations",
    "messages",
    "users",
    "labels",
    "tags",
    "taggings",
    "csat_survey_responses",
    "women_help_certificate_pools",
    "women_help_certificate_pool_aids",
    "women_help_pdf_certificates",
    "women_help_certificate_requests",
}
CAPTURED = datetime(2026, 11, 27, 12, tzinfo=UTC)


class Connection:
    """Record calls at the asyncpg boundary without starting a database."""

    def __init__(self, pages=()):
        self.pages = {table: iter(rows) for table, rows in dict(pages).items()}
        self.calls = []
        self.transaction_options = None
        self.in_transaction = False

    @asynccontextmanager
    async def transaction(self, **options):
        self.transaction_options = options
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    async def execute(self, sql, *params):
        assert self.in_transaction
        self.calls.append((sql, params))

    async def fetchval(self, sql, *params):
        assert self.in_transaction
        self.calls.append((sql, params))
        return CAPTURED

    async def fetch(self, sql, *params):
        assert self.in_transaction
        self.calls.append((sql, params))
        table = next(t for t, query in source.SELECTS.items() if query == sql)
        return next(self.pages.get(table, iter(())), [])

    def fetches(self, table):
        return [params for sql, params in self.calls if sql == source.SELECTS[table]]


@pytest.mark.parametrize(
    ("account_id", "inbox_ids", "excluded"),
    [(0, (2,), ()), (True, (2,), ()), (1, (), ()), (1, (-2,), ()), (1, (2,), ("7",))],
)
def test_scope_rejects_missing_or_nonpositive_ids(account_id, inbox_ids, excluded):
    with pytest.raises(ValueError):
        source.Scope(account_id, inbox_ids, excluded)


async def test_reads_inside_bounded_read_only_transaction():
    connection = Connection()
    snap = await source.read_snapshot(connection, source.Scope(1, (2,)))

    assert connection.transaction_options == {"isolation": "repeatable_read", "readonly": True}
    assert connection.calls[0][0].startswith("SET LOCAL statement_timeout")
    assert connection.calls[1][0].startswith("SET LOCAL lock_timeout")
    assert "transaction_timestamp()" in connection.calls[2][0]
    assert snap.captured_at == CAPTURED
    assert set(snap.tables) == snap.complete_tables == TABLES
    assert snap.tables["labels"] == snap.tables["tags"] == snap.tables["taggings"] == []
    assert connection.fetches("labels") == connection.fetches("tags") == []


async def test_pages_by_id_and_pool_aids_by_composite_key():
    connection = Connection(
        {
            "conversations": [[{"id": 10}, {"id": 20}], [{"id": 110}]],
            "women_help_certificate_pool_aids": [
                [{"pool_id": 4, "aid_id": "a"}, {"pool_id": 4, "aid_id": "b"}],
                [{"pool_id": 4, "aid_id": "c"}],
            ],
        }
    )
    snap = await source.read_snapshot(connection, source.Scope(3, (5,), (8,)), batch_size=2)

    assert [row["id"] for row in snap.tables["conversations"]] == [10, 20, 110]
    assert snap.changed_conversation_ids == {10, 20, 110}
    assert connection.fetches("conversations")[1][-2:] == (20, 2)
    assert connection.fetches("women_help_certificate_pool_aids")[1][-3:] == (4, "b", 2)
    assert [row["aid_id"] for row in snap.tables["women_help_certificate_pool_aids"]] == [
        "a",
        "b",
        "c",
    ]


async def test_delta_passes_same_utc_cutoff_to_conversations_and_messages():
    older_row = {"id": 14, "conversation_id": 6, "created_at": datetime.fromisoformat("2020-01-01")}
    connection = Connection({"messages": [[older_row]]})
    cutoff = datetime(2026, 11, 25, 10, tzinfo=timezone(timedelta(hours=3)))
    snap = await source.read_snapshot(connection, source.Scope(3, (5,), (8,)), since=cutoff)

    assert snap.tables["messages"] == [older_row]
    assert snap.changed_conversation_ids == set()
    assert snap.complete_tables == TABLES - {"messages", "conversations"}
    for table in ("conversations", "messages"):
        utc_naive = datetime.fromisoformat("2026-11-25T07:00:00")
        assert connection.fetches(table)[0][:4] == (3, (5,), (8,), utc_naive)
        sql = source.SELECTS[table]
        assert "created_at >= $4" in sql
        assert "updated_at >= $4" in sql
    assert cutoff not in connection.fetches("women_help_certificate_requests")[0]


@pytest.mark.parametrize("batch_size", [0, 1001, False])
async def test_refuses_unbounded_batches(batch_size):
    connection = Connection()
    with pytest.raises(ValueError):
        await source.read_snapshot(connection, source.Scope(1, (2,)), batch_size=batch_size)
    assert connection.calls == []


async def test_refuses_to_return_a_partial_snapshot_after_row_limit(monkeypatch):
    monkeypatch.setattr(source, "MAX_ROWS", 1)
    connection = Connection({"inboxes": [[{"id": 1}, {"id": 2}]]})
    with pytest.raises(RuntimeError, match="row limit"):
        await source.read_snapshot(connection, source.Scope(1, (2,)))


async def test_refuses_to_return_partial_snapshot_after_projected_json_byte_limit(monkeypatch):
    monkeypatch.setattr(source, "MAX_JSON_BYTES", 16)
    connection = Connection(
        {
            "conversations": [
                [
                    {
                        "id": 9,
                        "scenario_requests": '{"one": {}, "two": {}}',
                    }
                ]
            ]
        }
    )
    with pytest.raises(RuntimeError, match="JSON byte limit"):
        await source.read_snapshot(connection, source.Scope(1, (2,)))


async def test_refuses_a_json_projection_that_hit_the_database_payload_limit():
    connection = Connection({"conversations": [[{"id": 9, "scenario_followups": "true"}]]})
    with pytest.raises(RuntimeError, match="JSON payload limit"):
        await source.read_snapshot(connection, source.Scope(1, (2,)))


async def test_preserves_sanitized_malformed_projection_and_accepts_decoded_json():
    connection = Connection(
        {
            "conversations": [
                [
                    {
                        "id": 9,
                        "scenario_requests": '{"req-1": {"state": "completed"}, "bad-entry": []}',
                        "scenario_followups": [],
                    }
                ]
            ],
            "women_help_certificate_requests": [[{"id": 1, "source_event_keys": '["event-1"]'}]],
        }
    )
    snap = await source.read_snapshot(connection, source.Scope(3, (5,)))
    assert snap.tables["conversations"][0]["scenario_requests"] == {
        "req-1": {"state": "completed"},
        "bad-entry": [],
    }
    assert snap.tables["conversations"][0]["scenario_followups"] == []
    assert snap.tables["women_help_certificate_requests"][0]["source_event_keys"] == ["event-1"]


async def test_selects_scope_global_and_certificate_rows_with_bound_parameters():
    connection = Connection()
    await source.read_snapshot(connection, source.Scope(3, (5,), (8,), ("approved",)))
    for table in ("taggings", "tags", "labels"):
        assert "approved" not in source.SELECTS[table]
        assert ("approved",) in connection.fetches(table)[0]
    for table in ("messages", "women_help_pdf_certificates", "tags"):
        sql = source.SELECTS[table]
        assert "scoped_conversations" in sql
        assert "account_id = $1" in sql
        assert "inbox_id = ANY($2" in sql
        assert "ANY($3" in sql
    certificates = source.SELECTS["women_help_pdf_certificates"]
    assert "record_kind = 'historical_issuance'" in certificates
    assert "record_kind = 'certificate'" in certificates
    assert "chatwoot_message_id" in certificates
    assert "$1::bigint AS account_id" in certificates
    assert "sc.resolved_contact_id, sc.resolved_conversation_id" in certificates
    assert "account_users" in source.SELECTS["users"]


async def test_queue_scope_from_postgres_fixture(database):
    from pathlib import Path

    await database.execute(Path(__file__).with_name("source_fixture.sql").read_text())
    scope = source.Scope(3, (5,), (8,))
    snapshot = await source.read_snapshot(database, scope, batch_size=2)
    requests = {row["id"]: row for row in snapshot.tables["women_help_certificate_requests"]}
    assert set(requests) == {1, 3, 4, 5, 6}
    first = datetime(2026, 11, 1, 10, tzinfo=UTC)
    last = datetime(2026, 11, 27, 11, tzinfo=UTC)

    for ident, endpoints, events_scope, events in (
        (1, (2, 1, first, None, None, None), "partial", []),
        (3, (2, 1, first, 2, 2, last), "complete", ["req-3", "req-3"]),
        (4, (2, 1, first, 2, 2, last), "invalid", []),
        (5, (None, None, None, 2, 2, last), "partial", []),
        (6, (2, 1, first, 2, 2, last), "invalid", []),
    ):
        row = requests[ident]
        assert (
            row["conversation_id"],
            row["source_message_id"],
            row["first_requested_at"],
            row["last_conversation_id"],
            row["last_source_message_id"],
            row["last_requested_at"],
        ) == endpoints
        assert row["source_event_keys_scope"] == events_scope
        assert row["source_event_keys"] == events


def test_selects_only_metadata_and_projects_json_in_postgres():
    assert set(source.SELECTS) == TABLES
    for sql in source.SELECTS.values():
        assert "SELECT *" not in sql.upper()
        for forbidden in (
            "email",
            "phone_number",
            "content_attributes",
            "activation_code",
            "serial_number",
            "recipient_hash",
            "pdf_object_key",
            "pdf_sha256",
            "pdf_filename",
            "pdf_bucket",
            "feedback_message",
        ):
            assert forbidden not in sql.lower()
    conversations = source.SELECTS["conversations"]
    assert "jsonb_object_agg" in conversations
    assert "jsonb_build_object" in conversations
    assert "ELSE '[]'::jsonb" in conversations
    assert "c.custom_attributes," not in conversations
    queue = source.SELECTS["women_help_certificate_requests"]
    assert "jsonb_array_elements" in queue
    assert "jsonb_typeof" in queue


async def test_queue_intermediate_attempt_cannot_escape_inbox_scope(database):
    from pathlib import Path

    await database.execute(Path(__file__).with_name("source_fixture.sql").read_text())
    # First and last requests are in inbox5, but the middle event cannot be attributed.
    await database.execute(
        "UPDATE women_help_certificate_requests SET source_event_keys="
        '\'["first-a","middle-b","last-a"]\'::jsonb WHERE id=3'
    )
    snap = await source.read_snapshot(database, source.Scope(3, (5,), (8,)))
    rows = {r["id"]: r for r in snap.tables["women_help_certificate_requests"]}
    assert rows[3]["source_event_keys"] == []
    assert rows[3]["source_event_keys_scope"] == "partial"
    assert rows[1]["aid_id"] is None and rows[1]["reason"] is None

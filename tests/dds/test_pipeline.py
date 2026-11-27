from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import pytest

from analytics.dds.catalog import load_catalog
from analytics.dds.store import ContractError, migrate, publish, read_state
from analytics.dds.transform import project

KEY = b"x" * 32
NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


def scope():
    return SimpleNamespace(account_id=1, inbox_ids=(5,), excluded_contact_ids=(), label_titles=())


def snapshot(*, state="requested", requests=True, complete=True, time=NOW):
    return SimpleNamespace(
        tables={
            "contacts": [{"id": 1, "created_at": NOW}],
            "inboxes": [{"id": 5}],
            "conversations": [
                {
                    "id": 10,
                    "inbox_id": 5,
                    "contact_id": 1,
                    "status": 2,
                    "scenario_requests": {"r": {"state": state, "aid_id": "legal_consultation"}}
                    if requests
                    else {},
                }
            ],
        },
        complete_tables={"contacts", "inboxes", "conversations"} if complete else set(),
        changed_conversation_ids={10},
        captured_at=time,
    )


@pytest.mark.asyncio
async def test_schema_and_comments_match_catalog_and_migration_repeats(database):
    await migrate(database)
    await migrate(database)
    assert await read_state(database) is None
    for name, table in load_catalog()["tables"].items():
        comment = await database.fetchval("SELECT obj_description($1::regclass)", "dds." + name)
        assert comment
        actual = await database.fetch(
            """SELECT a.attname,col_description(a.attrelid,a.attnum) c
          FROM pg_attribute a WHERE a.attrelid=$1::regclass AND a.attnum>0""",
            "dds." + name,
        )
        assert all(r["c"] for r in actual)
        assert set(table["columns"]).issubset({r["attname"] for r in actual})


@pytest.mark.asyncio
async def test_initial_repeat_change_and_observed_history(database):
    await migrate(database)
    s = snapshot()
    p = project(s, scope(), KEY)
    await publish(database, p, s, scope(), KEY)
    await publish(database, p, s, scope(), KEY)
    assert await database.fetchval("SELECT count(*) FROM dds.fact_consultation_request") == 1
    assert await database.fetchval("SELECT count(*) FROM ctl.observed_state_history") == 2
    assert (await read_state(database))["watermark"] == NOW
    s2 = snapshot(state="completed", time=NOW + timedelta(hours=1))
    await publish(database, project(s2, scope(), KEY), s2, scope(), KEY)
    assert await database.fetchval("SELECT state FROM dds.fact_consultation_request") == "completed"
    assert await database.fetchval("SELECT count(*) FROM ctl.observed_state_history") == 3


@pytest.mark.asyncio
async def test_missing_child_tombstoned_on_changed_parent_not_other_parent(database):
    await migrate(database)
    s = snapshot()
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    s2 = snapshot(requests=False, complete=False, time=NOW + timedelta(hours=1))
    await publish(database, project(s2, scope(), KEY), s2, scope(), KEY)
    assert await database.fetchval(
        "SELECT deleted_at IS NOT NULL FROM dds.fact_consultation_request"
    )
    assert await database.fetchval("SELECT deleted_at FROM dds.dim_contact") is None


@pytest.mark.asyncio
async def test_scope_or_identity_rotation_requires_rebuild(database):
    await migrate(database)
    s = snapshot()
    p = project(s, scope(), KEY)
    await publish(database, p, s, scope(), KEY)
    with pytest.raises(ContractError):
        await publish(database, p, s, scope(), b"y" * 32)
    sc = scope()
    sc.inbox_ids = (6,)
    with pytest.raises(ContractError):
        await publish(database, p, s, sc, KEY)
    assert await database.fetchval("SELECT count(*) FROM ctl.etl_run") == 1


@pytest.mark.asyncio
async def test_publish_failure_rolls_back_data_history_and_watermark(database):
    await migrate(database)
    s = snapshot()
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    await database.execute("""CREATE FUNCTION ctl.reject_run() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'synthetic publish failure'; END $$;
      CREATE TRIGGER reject_run BEFORE INSERT ON ctl.etl_run FOR EACH ROW
        EXECUTE FUNCTION ctl.reject_run();""")
    s2 = snapshot(state="completed", time=NOW + timedelta(hours=1))
    with pytest.raises(asyncpg.RaiseError):
        await publish(database, project(s2, scope(), KEY), s2, scope(), KEY)
    assert await database.fetchval("SELECT state FROM dds.fact_consultation_request") == "requested"
    assert (await read_state(database))["watermark"] == NOW
    assert await database.fetchval("SELECT count(*) FROM ctl.observed_state_history") == 2


@pytest.mark.asyncio
async def test_corrupt_previously_valid_child_does_not_erase_history(database):
    await migrate(database)
    s = snapshot()
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    s2 = snapshot(state="invalid", time=NOW + timedelta(hours=1))
    with pytest.raises(ContractError):
        await publish(database, project(s2, scope(), KEY), s2, scope(), KEY)
    assert await database.fetchval("SELECT deleted_at FROM dds.fact_consultation_request") is None
    assert (await read_state(database))["watermark"] == NOW


@pytest.mark.asyncio
async def test_reconciliation_tombstones_missing_entities_and_resurrects(database):
    await migrate(database)
    s = snapshot()
    p = project(s, scope(), KEY)
    await publish(database, p, s, scope(), KEY)
    empty = SimpleNamespace(
        tables={"contacts": []},
        complete_tables={"contacts"},
        changed_conversation_ids=set(),
        captured_at=NOW + timedelta(hours=1),
    )
    await publish(database, project(empty, scope(), KEY), empty, scope(), KEY)
    assert await database.fetchval("SELECT deleted_at IS NOT NULL FROM dds.dim_contact")
    restored = snapshot(time=NOW + timedelta(hours=2))
    await publish(database, project(restored, scope(), KEY), restored, scope(), KEY)
    assert await database.fetchval("SELECT deleted_at FROM dds.dim_contact") is None


@pytest.mark.asyncio
async def test_incompatible_schema_is_rejected(database):
    await migrate(database)
    await database.execute("ALTER TABLE dds.fact_message DROP COLUMN status")
    with pytest.raises(ContractError):
        await migrate(database)


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_revert_newer_publication(database):
    await migrate(database)
    old = snapshot()
    new = snapshot(state="completed", time=NOW + timedelta(hours=1))
    await publish(database, project(new, scope(), KEY), new, scope(), KEY)
    with pytest.raises(ContractError):
        await publish(database, project(old, scope(), KEY), old, scope(), KEY)
    assert await database.fetchval("SELECT state FROM dds.fact_consultation_request") == "completed"


@pytest.mark.asyncio
async def test_full_reconcile_deletes_children_of_physically_removed_conversation(database):
    await migrate(database)
    s = snapshot()
    s.tables["conversations"][0]["scenario_followups"] = {
        "f": {"state": "pending", "context": {"request_key": "r"}}
    }
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    empty = SimpleNamespace(
        tables={"conversations": []},
        complete_tables={"conversations"},
        changed_conversation_ids=set(),
        captured_at=NOW + timedelta(hours=1),
    )
    await publish(database, project(empty, scope(), KEY), empty, scope(), KEY)
    for table in ("fact_conversation", "fact_consultation_request", "fact_followup"):
        assert (
            await database.fetchval(f"SELECT count(*) FROM dds.{table} WHERE deleted_at IS NULL")
            == 0
        )


@pytest.mark.asyncio
async def test_aid_removed_only_after_all_contributing_sources_are_complete(database):
    await migrate(database)
    s = snapshot()
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    delta = snapshot(requests=False, complete=False, time=NOW + timedelta(hours=1))
    await publish(database, project(delta, scope(), KEY), delta, scope(), KEY)
    assert await database.fetchval("SELECT count(*) FROM dds.dim_aid WHERE deleted_at IS NULL") == 1
    delta.captured_at = NOW + timedelta(hours=2)
    delta.complete_tables = {
        "conversations",
        "women_help_certificate_pool_aids",
        "women_help_pdf_certificates",
        "women_help_certificate_requests",
    }
    await publish(database, project(delta, scope(), KEY), delta, scope(), KEY)
    assert await database.fetchval("SELECT count(*) FROM dds.dim_aid WHERE deleted_at IS NULL") == 0


@pytest.mark.asyncio
async def test_same_source_version_new_values_keep_latest_with_dq(database):
    await migrate(database)
    s = snapshot()
    s.tables["messages"] = [
        {"id": 20, "conversation_id": 10, "inbox_id": 5, "status": 0, "updated_at": NOW}
    ]
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    s.captured_at = NOW + timedelta(hours=1)
    s.tables["messages"][0]["status"] = 1
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    assert await database.fetchval("SELECT status FROM dds.fact_message") == 1
    assert (
        await database.fetchval(
            "SELECT count(*) FROM ctl.dq_issue WHERE code='unchanged_source_version'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_malformed_existing_queue_event_list_cannot_remove_bridge(database):
    await migrate(database)
    s = snapshot()
    s.tables["women_help_certificate_requests"] = [
        {
            "id": 3,
            "status": "waiting",
            "source_event_keys": ["event"],
            "source_event_keys_scope": "complete",
        }
    ]
    s.complete_tables.add("women_help_certificate_requests")
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    s.captured_at = NOW + timedelta(hours=1)
    s.tables["women_help_certificate_requests"][0].update(
        source_event_keys=[], source_event_keys_scope="invalid"
    )
    with pytest.raises(ContractError):
        await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    assert (
        await database.fetchval(
            "SELECT count(*) FROM dds.bridge_certificate_request_event WHERE deleted_at IS NULL"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_reader_projection_publication_incremental_and_full_deletion(database):
    from pathlib import Path

    from analytics.dds.pipeline import run_once
    from analytics.dds.source import Scope

    await database.execute(Path(__file__).with_name("source_fixture.sql").read_text())
    await migrate(database)
    sc = Scope(3, (5,), (8,), ("approved",))
    first = await run_once(database, database, sc, KEY)
    assert first["rows"]["fact_message"] == 2
    assert first["rows"]["fact_certificate"] == 2
    assert (
        await database.fetchval(
            "SELECT count(*) FROM dds.fact_certificate WHERE resolved_contact_key IS NOT NULL"
        )
        == 2
    )
    again = await run_once(database, database, sc, KEY)
    assert again["rows"]["fact_message"] == 1
    assert (
        await database.fetchval("SELECT count(*) FROM dds.fact_message WHERE deleted_at IS NULL")
        == 2
    )
    await database.execute("DELETE FROM public.conversations WHERE id=2")
    await run_once(database, database, sc, KEY, full=True)
    for table in (
        "fact_conversation",
        "fact_consultation_request",
        "fact_followup",
        "fact_message",
        "fact_certificate",
    ):
        assert (
            await database.fetchval(f"SELECT count(*) FROM dds.{table} WHERE deleted_at IS NULL")
            == 0
        )


@pytest.mark.asyncio
async def test_history_round_trips_do_not_grow_per_fact(database):
    class Counted:
        def __init__(self, conn):
            self.conn = conn
            self.calls = 0

        def __getattr__(self, name):
            method = getattr(self.conn, name)
            if name not in (
                "execute",
                "fetch",
                "fetchrow",
                "fetchval",
                "executemany",
                "copy_records_to_table",
            ):
                return method

            async def call(*a, **k):
                self.calls += 1
                return await method(*a, **k)

            return call

    await migrate(database)
    counts = []
    for n in (100, 200):
        s = snapshot(time=NOW + timedelta(hours=n))
        s.tables["conversations"] = [
            {"id": i, "contact_id": 1, "inbox_id": 5, "status": 2} for i in range(1, n + 1)
        ]
        conn = Counted(database)
        await publish(conn, project(s, scope(), KEY), s, scope(), KEY)
        counts.append(conn.calls)
    assert counts[1] <= counts[0] + 10


@pytest.mark.asyncio
async def test_missing_nullable_foreign_key_is_null_with_dq(database):
    await migrate(database)
    s = snapshot()
    s.tables["conversations"][0]["assignee_id"] = 999
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    assert await database.fetchval("SELECT assignee_staff_key FROM dds.fact_conversation") is None
    assert (
        await database.fetchval(
            "SELECT count(*) FROM ctl.dq_issue WHERE code='unresolved_assignee_staff_key'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_sql_examples_execute_and_show_missing_date_coverage(database):
    from pathlib import Path

    await database.execute(Path(__file__).with_name("source_fixture.sql").read_text())
    await migrate(database)
    from analytics.dds.pipeline import run_once
    from analytics.dds.source import Scope

    await run_once(database, database, Scope(3, (5,), (8,), ("approved",)), KEY)
    sql = Path("analytics/dds/examples.sql").read_text()
    results = [await database.fetch(statement) for statement in sql.split(";") if statement.strip()]
    assert len(results) == 10
    assert results[7][0]["actual_help"] is None
    assert results[8][0]["all_active_scoped_requests"] == 1
    assert results[8][0]["completed_missing_date"] == 1


@pytest.mark.asyncio
async def test_analyst_role_reads_only_dds_and_has_query_limits(database):
    import os
    from pathlib import Path
    from uuid import uuid4

    await database.execute(Path(__file__).with_name("source_fixture.sql").read_text())
    await migrate(database)
    group = "dds_analyst_" + uuid4().hex[:12]
    reader = "dds_reader_" + uuid4().hex[:12]
    db = await database.fetchval("SELECT current_database()")
    sql = (
        Path("analytics/dds/access.sql")
        .read_text()
        .replace("dds_analyst", group)
        .replace("dds_reader", reader)
        .replace(':"database"', '"' + db + '"')
    )
    await database.execute(sql)
    connection = await asyncpg.connect(os.environ["TEST_DDS_POSTGRES"], database=db, user=reader)
    try:
        assert await connection.fetchval("SHOW statement_timeout") == "15s"
        assert await connection.fetchval("SELECT count(*) FROM dds.fact_message") == 0
        for table in ("public.messages", "public.contacts", "ctl.etl_run"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetchval("SELECT count(*) FROM " + table)
    finally:
        await connection.close()
        await database.execute(f"DROP OWNED BY {reader},{group}")
        await database.execute(f"DROP ROLE {reader}; DROP ROLE {group}")


@pytest.mark.asyncio
async def test_published_sender_keys_resolve_only_in_their_own_namespace(database):
    from analytics.dds.transform import entity_key

    await migrate(database)
    s = snapshot()
    s.tables["users"] = [{"id": 2}]
    s.tables["messages"] = [
        {"id": n, "conversation_id": 10, "inbox_id": 5, "sender_type": kind, "sender_id": sid}
        for n, kind, sid in [(1, "Contact", 1), (2, "User", 2), (3, "AgentBot", 9)]
    ]
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    found = {
        r["sender_type"]: r["sender_key"]
        for r in await database.fetch("SELECT sender_type,sender_key FROM dds.fact_message")
    }
    assert found == {
        "Contact": entity_key(KEY, 1, "contact", 1),
        "User": entity_key(KEY, 1, "staff", 2),
        "AgentBot": entity_key(KEY, 1, "bot", 9),
    }
    assert (
        await database.fetchval(
            "SELECT count(*) FROM ctl.dq_issue WHERE code='unresolved_sender_key'"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_role_template_rejects_public_column_select(database):
    from pathlib import Path
    from uuid import uuid4

    await migrate(database)
    await database.execute(
        "CREATE TABLE public.raw_column_test (value text); GRANT SELECT(value) ON public.raw_column_test TO PUBLIC"
    )
    suffix = uuid4().hex[:12]
    db = await database.fetchval("SELECT current_database()")
    sql = (
        Path("analytics/dds/access.sql")
        .read_text()
        .replace("dds_analyst", "dds_analyst_" + suffix)
        .replace("dds_reader", "dds_reader_" + suffix)
        .replace(':"database"', '"' + db + '"')
    )
    with pytest.raises(asyncpg.RaiseError):
        await database.execute(sql)
    await database.execute("ROLLBACK")


@pytest.mark.asyncio
async def test_untouched_foreign_reference_cleanup_is_deferred_to_full(database):
    from analytics.dds.transform import entity_key

    await migrate(database)
    s = snapshot()
    s.tables["users"] = [{"id": 2}]
    s.tables["conversations"][0]["assignee_id"] = 2
    s.complete_tables.add("users")
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    delta = SimpleNamespace(
        tables={"users": []},
        complete_tables={"users"},
        changed_conversation_ids=set(),
        captured_at=NOW + timedelta(hours=1),
    )
    await publish(database, project(delta, scope(), KEY), delta, scope(), KEY)
    assert await database.fetchval(
        "SELECT assignee_staff_key FROM dds.fact_conversation"
    ) == entity_key(KEY, 1, "staff", 2)
    assert await database.fetchval("SELECT deleted_at IS NOT NULL FROM dds.dim_staff")
    s.captured_at = NOW + timedelta(hours=2)
    s.tables["users"] = []
    await publish(database, project(s, scope(), KEY), s, scope(), KEY)
    assert await database.fetchval("SELECT assignee_staff_key FROM dds.fact_conversation") is None


@pytest.mark.asyncio
async def test_role_template_rejects_public_column_update(database):
    from pathlib import Path
    from uuid import uuid4

    await migrate(database)
    await database.execute(
        "CREATE TABLE public.raw_write_test (value text); GRANT UPDATE(value) ON public.raw_write_test TO PUBLIC"
    )
    suffix = uuid4().hex[:12]
    db = await database.fetchval("SELECT current_database()")
    sql = (
        Path("analytics/dds/access.sql")
        .read_text()
        .replace("dds_analyst", "dds_analyst_" + suffix)
        .replace("dds_reader", "dds_reader_" + suffix)
        .replace(':"database"', '"' + db + '"')
    )
    with pytest.raises(asyncpg.RaiseError):
        await database.execute(sql)
    await database.execute("ROLLBACK")

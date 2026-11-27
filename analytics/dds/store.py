"""Transactional DDS publication; accepts a pre-projected bounded snapshot."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg

from analytics.dds.catalog import load_catalog, schema_sql, validate_catalog
from analytics.dds.transform import SPECS, Issue, Projection, entity_key, row_hash

LOCK_ID = 836473800323389031
HISTORY_FIELDS = {
    "fact_conversation": ("status", "assignee_staff_key", "assignee_bot_key", "ai_assignee_type"),
    "fact_consultation_request": ("state", "aid_id", "specialist_staff_key"),
    "fact_followup": ("state",),
    "fact_certificate": ("status", "selected_aid_id"),
    "fact_certificate_request": ("status", "reason", "aid_id", "fulfilled_certificate_key"),
}
JSON_CHILDREN = ("fact_consultation_request", "fact_followup")
CTL_SQL = """
CREATE SCHEMA IF NOT EXISTS ctl;
REVOKE ALL ON SCHEMA ctl FROM PUBLIC;
CREATE TABLE IF NOT EXISTS ctl.schema_version (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 version integer NOT NULL, catalog_hash text NOT NULL);
CREATE TABLE IF NOT EXISTS ctl.pipeline_state (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 scope_digest text NOT NULL, key_digest text NOT NULL, watermark timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS ctl.etl_run (
 run_id uuid PRIMARY KEY, captured_at timestamptz NOT NULL, published_at timestamptz NOT NULL,
 mode text NOT NULL, row_counts jsonb NOT NULL, issue_count integer NOT NULL);
CREATE TABLE IF NOT EXISTS ctl.dq_issue (
 run_id uuid NOT NULL REFERENCES ctl.etl_run(run_id), table_name text NOT NULL,
 entity_key uuid, code text NOT NULL);
CREATE TABLE IF NOT EXISTS ctl.observed_state_history (
 table_name text NOT NULL, entity_key uuid NOT NULL, observed_at timestamptz NOT NULL,
 state_hash text NOT NULL, state jsonb NOT NULL,
 PRIMARY KEY(table_name,entity_key,observed_at,state_hash));
REVOKE ALL ON ALL TABLES IN SCHEMA ctl FROM PUBLIC;
"""


class ContractError(ValueError):
    """A schema, identity, scope or consistency invariant was violated."""


def _catalog_digest():
    return row_hash(load_catalog())


def scope_digest(scope) -> str:
    return row_hash(
        {
            "account": scope.account_id,
            "inboxes": sorted(set(scope.inbox_ids)),
            "excluded": sorted(set(scope.excluded_contact_ids)),
            "labels": sorted(set(scope.label_titles)),
        }
    )


@asynccontextmanager
async def load_lock(connection):
    if not await connection.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID):
        raise ContractError("another DDS run is active")
    try:
        yield
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)


async def _writable(connection):
    if await connection.fetchval("SELECT pg_is_in_recovery()"):
        raise ContractError("DDS target must be writable primary; standby is for reads")


async def _verify_columns(connection):
    types = {
        "timestamptz": "timestamp with time zone",
        "integer": "integer",
        "bigint": "bigint",
        "uuid": "uuid",
        "text": "text",
        "date": "date",
        "boolean": "boolean",
    }
    for table, spec in load_catalog()["tables"].items():
        actual = {
            r["column_name"]: (r["data_type"], r["is_nullable"] == "YES")
            for r in await connection.fetch(
                """SELECT column_name,data_type,is_nullable
                    FROM information_schema.columns WHERE table_schema='dds' AND table_name=$1""",
                table,
            )
        }
        required = {
            **spec["columns"],
            "row_hash": {"type": "text", "nullable": False},
            "loaded_at": {"type": "timestamptz", "nullable": False},
            "deleted_at": {"type": "timestamptz", "nullable": True},
        }
        if actual != {c: (types[f["type"]], f["nullable"]) for c, f in required.items()}:
            raise ContractError("DDS schema does not match the versioned catalog")
        keys = await connection.fetch(
            """SELECT a.attname FROM pg_index i
          CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
          JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
          WHERE i.indrelid=$1::regclass AND i.indisprimary ORDER BY k.ord""",
            "dds." + table,
        )
        if [r["attname"] for r in keys] != spec["primary_key"]:
            raise ContractError("DDS primary key differs from catalog")


async def migrate(connection):
    """Explicit schema operation, never invoked implicitly by load()."""
    if validate_catalog():
        raise ContractError("invalid DDS catalog")
    await _writable(connection)
    async with load_lock(connection):
        try:
            async with connection.transaction():
                await connection.execute(CTL_SQL)
                old = await connection.fetchrow(
                    "SELECT version,catalog_hash FROM ctl.schema_version"
                )
                if old and (
                    old["version"] != load_catalog()["version"]
                    or old["catalog_hash"] != _catalog_digest()
                ):
                    raise ContractError(
                        "schema migration version differs; explicit upgrade required"
                    )
                await connection.execute(schema_sql())
                await _verify_columns(connection)
                await connection.execute(
                    """INSERT INTO ctl.schema_version(version,catalog_hash)
                  VALUES($1,$2) ON CONFLICT(singleton) DO NOTHING""",
                    load_catalog()["version"],
                    _catalog_digest(),
                )
        except ContractError:
            raise
        except asyncpg.PostgresError as exc:
            raise ContractError("DDS migration failed: " + type(exc).__name__) from None


async def read_state(connection):
    try:
        row = await connection.fetchrow("SELECT * FROM ctl.pipeline_state")
        return dict(row) if row else None
    except asyncpg.PostgresError as exc:
        raise ContractError("DDS not initialized: " + type(exc).__name__) from None


async def check_contract(connection, scope, key):
    version = await connection.fetchrow("SELECT version,catalog_hash FROM ctl.schema_version")
    if (
        not version
        or version["version"] != load_catalog()["version"]
        or (version["catalog_hash"] != _catalog_digest())
    ):
        raise ContractError("DDS catalog migration required")
    await _verify_columns(connection)
    state = await read_state(connection)
    if state and (
        state["scope_digest"] != scope_digest(scope)
        or state["key_digest"] != hashlib.sha256(key).hexdigest()
    ):
        raise ContractError("scope or identity key changed; controlled rebuild required")
    return state


def _state_expression(table, alias, deleted=False):
    parts = [f"'{name}', {alias}.{name}" for name in HISTORY_FIELDS[table]]
    parts.append("'deleted', " + ("true" if deleted else "false"))
    return "jsonb_build_object(" + ",".join(parts) + ")"


async def _stage_history(connection, table, temp, observed_at, definition):
    if table not in HISTORY_FIELDS:
        return
    pk = definition["primary_key"][0]
    before = _state_expression(table, "d").replace(
        "'deleted', false", "'deleted', d.deleted_at IS NOT NULL"
    )
    after = _state_expression(table, "s")
    await connection.execute(
        f"""INSERT INTO ctl.observed_state_history
      (table_name,entity_key,observed_at,state_hash,state)
      SELECT $1,s.{pk},$2,md5(({after})::text),{after} FROM {temp} s
      LEFT JOIN dds.{table} d ON d.{pk}=s.{pk}
      WHERE d.{pk} IS NULL OR ({before}) IS DISTINCT FROM ({after})
      ON CONFLICT DO NOTHING""",
        table,
        observed_at,
    )


def _complete_targets(snapshot):
    complete = {
        target for source, (target, *_rest) in SPECS.items() if source in snapshot.complete_tables
    }
    if {
        "conversations",
        "women_help_certificate_pool_aids",
        "women_help_pdf_certificates",
        "women_help_certificate_requests",
    } <= snapshot.complete_tables:
        complete.add("dim_aid")
    if "conversations" in snapshot.complete_tables:
        complete.update(JSON_CHILDREN)
    if "women_help_certificate_pool_aids" in snapshot.complete_tables:
        complete.add("bridge_pool_aid")
    if "women_help_certificate_requests" in snapshot.complete_tables:
        complete.add("bridge_certificate_request_event")
    if "taggings" in snapshot.complete_tables:
        complete.add("bridge_object_tag")
    if {"tags", "labels"} <= snapshot.complete_tables:
        complete.add("bridge_label_tag")
    return complete


async def _resolve_links(connection, projection, snapshot, scope, issues):
    """Resolve against the proposed complete slice, retaining old keys only for deltas."""
    catalog = load_catalog()["tables"]
    tables = {t: [dict(r) for r in projection.tables.get(t, [])] for t in catalog}
    complete = _complete_targets(snapshot)
    available = {}
    for target, spec in catalog.items():
        keys = spec["primary_key"]
        found = {tuple(r[c] for c in keys) for r in tables[target]}
        if target not in complete:
            existing = await connection.fetch(
                f"SELECT {','.join(keys)} FROM dds.{target} "
                "WHERE account_id=$1 AND deleted_at IS NULL",
                scope.account_id,
            )
            found.update(tuple(r[c] for c in keys) for r in existing)
        available[target] = found
    for table, spec in catalog.items():
        retained = []
        for row in tables[table]:
            good = True
            relations = list(spec["relationships"])
            for relation in relations:
                target = relation["target_table"]
                if table == "fact_message" and relation.get("condition"):
                    expected = "Contact" if target == "dim_contact" else "User"
                    if row.get("sender_type") != expected:
                        continue
                if (
                    table == "fact_followup"
                    and relation.get("condition")
                    and row.get("target_kind") != "consultation"
                ):
                    continue
                if table == "bridge_object_tag" and relation.get("condition"):
                    expected = "Contact" if target == "dim_contact" else "Conversation"
                    if row.get("taggable_type") != expected:
                        continue
                columns = relation["columns"]
                values = tuple(row.get(c) for c in columns)
                if any(v is None for v in values) or values in available[target]:
                    continue
                ident = next(
                    (row.get(c) for c in spec["primary_key"] if isinstance(row.get(c), UUID)), None
                )
                name = next(c for c in columns if c != "account_id")
                issues.append(Issue(table, ident, "unresolved_" + name))
                for col in columns:
                    if col == "account_id":
                        continue
                    if not spec["columns"][col]["nullable"]:
                        good = False
                    else:
                        row[col] = None
            if good:
                retained.append(row)
        tables[table] = retained
    return Projection(tables, issues)


async def publish(connection, projection, snapshot, scope, key, *, batch_size=500):
    """Stage rows, then atomically merge, tombstone, log, and advance checkpoint."""
    if not 1 <= batch_size <= 1000:
        raise ContractError("invalid DDS batch size")
    await _writable(connection)
    if any(i.code in ("duplicate_conflict", "invalid_json_shape") for i in projection.issues):
        raise ContractError("ambiguous source snapshot; current DDS preserved")
    catalog = load_catalog()["tables"]
    issues = list(projection.issues)
    tables = list(catalog)
    staged = []
    async with load_lock(connection):
        state = await check_contract(connection, scope, key)
        if state and snapshot.captured_at < state["watermark"]:
            raise ContractError("stale source snapshot; current DDS preserved")
        else:
            watermark = snapshot.captured_at
        # A corrupt existing child must not be mistaken for a deletion or stale success.
        for i in projection.issues:
            if (
                i.table in JSON_CHILDREN
                and i.code
                in ("invalid_request_state", "invalid_followup_state", "invalid_entry_key")
                and i.entity_key
            ):
                pk = catalog[i.table]["primary_key"][0]
                if await connection.fetchval(
                    f"SELECT 1 FROM dds.{i.table} WHERE {pk}=$1 AND deleted_at IS NULL",
                    i.entity_key,
                ):
                    raise ContractError("existing child is invalid; current DDS preserved")
        for i in issues:
            if (
                i.code == "invalid_event_list"
                and i.entity_key
                and await connection.fetchval(
                    "SELECT 1 FROM dds.bridge_certificate_request_event "
                    "WHERE certificate_request_key=$1 AND deleted_at IS NULL LIMIT 1",
                    i.entity_key,
                )
            ):
                raise ContractError("queue event membership invalid; current DDS preserved")
        projection = await _resolve_links(connection, projection, snapshot, scope, issues)
        now = datetime.now(UTC)
        try:
            for table in tables:
                temp = "dds_stage_" + table
                await connection.execute(f"CREATE TEMP TABLE {temp} (LIKE dds.{table})")
                staged.append(temp)
                columns = list(catalog[table]["columns"]) + ["row_hash", "loaded_at", "deleted_at"]
                rows = projection.tables.get(table, [])
                for start in range(0, len(rows), batch_size):
                    records = [
                        tuple(row.get(c) for c in catalog[table]["columns"])
                        + (row_hash(row), now, None)
                        for row in rows[start : start + batch_size]
                    ]
                    await connection.copy_records_to_table(temp, records=records, columns=columns)
            async with connection.transaction():
                await connection.execute("SET LOCAL lock_timeout='1s'")
                await connection.execute("SET LOCAL statement_timeout='30s'")
                complete = _complete_targets(snapshot)
                changed = [
                    entity_key(key, scope.account_id, "conversation", cid)
                    for cid in snapshot.changed_conversation_ids
                ]
                for table, definition in catalog.items():
                    columns = list(definition["columns"]) + ["row_hash", "loaded_at", "deleted_at"]
                    keys = definition["primary_key"]
                    match = " AND ".join(f"d.{c}=s.{c}" for c in keys)
                    temp = "dds_stage_" + table
                    if "updated_at" in definition["columns"]:
                        pk = keys[0]
                        conflicts = await connection.fetch(f"""SELECT s.{pk} entity_key
                          FROM {temp} s JOIN dds.{table} d ON {match}
                          WHERE d.updated_at=s.updated_at AND d.row_hash<>s.row_hash
                            AND d.deleted_at IS NULL""")
                        issues.extend(
                            Issue(table, r["entity_key"], "unchanged_source_version")
                            for r in conflicts
                        )
                    await _stage_history(connection, table, temp, snapshot.captured_at, definition)
                    update = ",".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in keys)
                    await connection.execute(f"""INSERT INTO dds.{table} AS d ({",".join(columns)})
                      SELECT {",".join(columns)} FROM {temp} WHERE true
                      ON CONFLICT ({",".join(keys)}) DO UPDATE SET {update}
                      WHERE d.row_hash IS DISTINCT FROM EXCLUDED.row_hash OR d.deleted_at IS NOT NULL""")
                    predicate = None
                    params = [scope.account_id, now]
                    if table in complete:
                        predicate = "d.account_id=$1"
                    elif table in JSON_CHILDREN and changed:
                        predicate = "d.account_id=$1 AND d.conversation_key=ANY($3::uuid[])"
                        params.append(changed)
                    if predicate:
                        update_deleted = f"""UPDATE dds.{table} d SET deleted_at=$2
                          WHERE {predicate} AND d.deleted_at IS NULL
                          AND NOT EXISTS (SELECT 1 FROM {temp} s WHERE {match}) RETURNING d.*"""
                        if table in HISTORY_FIELDS:
                            state_expr = _state_expression(table, "d", deleted=True)
                            pk = definition["primary_key"][0]
                            await connection.execute(
                                f"""WITH removed AS ({update_deleted})
                              INSERT INTO ctl.observed_state_history
                               (table_name,entity_key,observed_at,state_hash,state)
                              SELECT '{table}',d.{pk},${len(params) + 1},md5(({state_expr})::text),
                                {state_expr} FROM removed d ON CONFLICT DO NOTHING""",
                                *params,
                                snapshot.captured_at,
                            )
                        else:
                            await connection.execute(update_deleted, *params)
                run_id = uuid4()
                counts = {t: len(projection.tables.get(t, [])) for t in tables}
                await connection.execute(
                    """INSERT INTO ctl.etl_run
                   VALUES($1,$2,$3,$4,$5::jsonb,$6)""",
                    run_id,
                    snapshot.captured_at,
                    now,
                    "full" if "messages" in snapshot.complete_tables else "incremental",
                    json.dumps(counts, sort_keys=True),
                    len(issues),
                )
                if issues:
                    await connection.executemany(
                        "INSERT INTO ctl.dq_issue VALUES($1,$2,$3,$4)",
                        [(run_id, i.table, i.entity_key, i.code) for i in issues],
                    )
                await connection.execute(
                    """INSERT INTO ctl.pipeline_state
                    (scope_digest,key_digest,watermark) VALUES($1,$2,$3)
                    ON CONFLICT(singleton) DO UPDATE SET watermark=EXCLUDED.watermark""",
                    scope_digest(scope),
                    hashlib.sha256(key).hexdigest(),
                    watermark,
                )
            return {
                "run_id": str(run_id),
                "rows": counts,
                "issues": len(issues),
                "captured_at": snapshot.captured_at.isoformat(),
            }
        finally:
            for temp in staged:
                await connection.execute(f"DROP TABLE IF EXISTS {temp}")

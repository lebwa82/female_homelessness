"""Read the approved Chatwoot metadata in one bounded, read-only snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asyncpg import Connection

MAX_ROWS = 50_000
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_BATCH_SIZE = 1000
_MAX_JSON_VALUE_BYTES = 16 * 1024
_MAX_PG_ID = 2**63 - 1


def _valid_id(value: int) -> bool:
    return type(value) is int and 0 < value <= _MAX_PG_ID


@dataclass(frozen=True)
class Scope:
    account_id: int
    inbox_ids: tuple[int, ...]
    excluded_contact_ids: tuple[int, ...] = ()
    label_titles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _valid_id(self.account_id):
            raise ValueError("account_id must be a positive integer")
        if not isinstance(self.inbox_ids, tuple) or not self.inbox_ids:
            raise ValueError("at least one inbox ID is required")
        if not all(_valid_id(value) for value in self.inbox_ids):
            raise ValueError("inbox IDs must be positive integers")
        if not isinstance(self.excluded_contact_ids, tuple) or not all(
            _valid_id(value) for value in self.excluded_contact_ids
        ):
            raise ValueError("excluded contact IDs must be positive integers")
        if not isinstance(self.label_titles, tuple) or not all(
            isinstance(title, str) and title.strip() and "\0" not in title
            for title in self.label_titles
        ):
            raise ValueError("label titles must be nonempty strings")


@dataclass
class Snapshot:
    tables: dict[str, list[dict]]
    complete_tables: set[str]
    changed_conversation_ids: set[int]
    captured_at: datetime


_SCOPED_CONVERSATIONS = """
scoped_conversations AS NOT MATERIALIZED (
    SELECT c.id, c.contact_id, c.inbox_id
    FROM public.conversations c
    JOIN public.inboxes i ON i.id = c.inbox_id AND i.account_id = $1::bigint
    JOIN public.contacts ct ON ct.id = c.contact_id AND ct.account_id = $1::bigint
    WHERE c.account_id = $1::bigint
      AND c.inbox_id = ANY($2::bigint[])
      AND NOT (c.contact_id = ANY($3::bigint[]))
)
"""
_SCOPED_CONTACT_INBOXES = """
scoped_contact_inboxes AS NOT MATERIALIZED (
    SELECT ci.id, ci.contact_id, ci.inbox_id
    FROM public.contact_inboxes ci
    JOIN public.inboxes i ON i.id = ci.inbox_id AND i.account_id = $1::bigint
    JOIN public.contacts ct ON ct.id = ci.contact_id AND ct.account_id = $1::bigint
    WHERE ci.inbox_id = ANY($2::bigint[])
      AND NOT (ci.contact_id = ANY($3::bigint[]))
)
"""
_SCOPED_CONTACTS = """
scoped_contacts AS NOT MATERIALIZED (
    SELECT ct.id
    FROM public.contacts ct
    WHERE ct.account_id = $1::bigint AND NOT (ct.id = ANY($3::bigint[]))
      AND (EXISTS (SELECT 1 FROM scoped_contact_inboxes ci WHERE ci.contact_id = ct.id)
           OR EXISTS (SELECT 1 FROM scoped_conversations c WHERE c.contact_id = ct.id))
)
"""
_SCOPED_MESSAGES = """
scoped_messages AS NOT MATERIALIZED (
    SELECT m.id, m.conversation_id, sc.contact_id
    FROM public.messages m
    JOIN scoped_conversations sc ON sc.id = m.conversation_id AND sc.inbox_id = m.inbox_id
    WHERE m.account_id = $1::bigint AND m.inbox_id = ANY($2::bigint[])
)
"""
_SCOPED_TAGGINGS = """
scoped_taggings AS NOT MATERIALIZED (
    SELECT tg.id, tg.tag_id, tg.taggable_id, tg.taggable_type, tg.context, tg.created_at
    FROM public.taggings tg
    JOIN public.tags t ON t.id = tg.tag_id AND t.name = ANY($4::text[])
    WHERE (
        tg.taggable_type = 'Conversation'
        AND EXISTS (SELECT 1 FROM scoped_conversations sc WHERE sc.id = tg.taggable_id)
    ) OR (
        tg.taggable_type = 'Contact'
        AND EXISTS (SELECT 1 FROM scoped_contacts ct WHERE ct.id = tg.taggable_id)
    )
)
"""
_SCOPED_CERTIFICATES = """
scoped_certificates AS NOT MATERIALIZED (
    SELECT cert.id, cert.pool_id, sc.contact_id AS resolved_contact_id,
           sc.id AS resolved_conversation_id
    FROM public.women_help_pdf_certificates cert
    JOIN scoped_conversations sc
      ON sc.id = cert.conversation_id AND sc.contact_id = cert.contact_id
    WHERE cert.account_id = $1::bigint AND cert.record_kind = 'historical_issuance'
    UNION ALL
    SELECT cert.id, cert.pool_id, sm.contact_id, sm.conversation_id
    FROM public.women_help_pdf_certificates cert
    JOIN scoped_messages sm ON sm.id = cert.chatwoot_message_id
    WHERE cert.record_kind = 'certificate'
)
"""
_SCOPED_QUEUE = """
scoped_queue AS NOT MATERIALIZED (
    SELECT req.id, req.pool_id, first_conv.id AS first_conversation_id,
           last_conv.id AS last_conversation_id
    FROM public.women_help_certificate_requests req
    LEFT JOIN scoped_conversations first_conv
      ON first_conv.id = req.conversation_id AND first_conv.contact_id = req.contact_id
    LEFT JOIN scoped_conversations last_conv
      ON last_conv.id = req.last_conversation_id AND last_conv.contact_id = req.contact_id
    WHERE req.account_id = $1::bigint AND NOT (req.contact_id = ANY($3::bigint[]))
      AND (first_conv.id IS NOT NULL OR last_conv.id IS NOT NULL)
)
"""
_SCOPED_POOLS = """
scoped_pools AS (
    SELECT pool_id FROM scoped_certificates
    UNION
    SELECT pool_id FROM scoped_queue
)
"""

# Permit compact codes or event identifiers, with date/id syntax for the other
# slots. Keep an empty-array marker for malformed values and container shapes.
_TOKEN = r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}"
_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]{4,35}Z?)?"
_INTEGER = r"-?[0-9]{1,20}"


def _scalar(value: str, pattern: str, *, numeric: bool = False) -> str:
    types = "'string', 'number'" if numeric else "'string'"
    return f"""
        CASE WHEN {value} IS NULL OR {value} = 'null'::jsonb THEN NULL
             WHEN jsonb_typeof({value}) IN ({types})
                  AND ({value} #>> '{{}}') ~ '^{pattern}$' THEN {value}
             ELSE '[]'::jsonb END
    """


def _project_entries(container: str, entries: str) -> str:
    return f"""
        CASE WHEN {container} IS NULL OR {container} = 'null'::jsonb THEN NULL
             WHEN jsonb_typeof({container}) = 'object' THEN (
                 SELECT COALESCE(jsonb_object_agg(
                     CASE WHEN entry.key ~ '^{_TOKEN}$' THEN entry.key ELSE '' END,
                     CASE WHEN entry.key ~ '^{_TOKEN}$'
                               AND jsonb_typeof(entry.value) = 'object' THEN {entries}
                          ELSE '[]'::jsonb END
                 ), '{{}}'::jsonb)
                 FROM jsonb_each({container}) entry
             )
             ELSE '[]'::jsonb END
    """


def _bounded_json(expression: str) -> str:
    # A top-level true is reserved to signal overflow. Every real projection
    # is an object, array or null. Bound each SELECT payload before transmission.
    return f"""
        (SELECT CASE WHEN octet_length(projected.value::text) > {_MAX_JSON_VALUE_BYTES}
                     THEN 'true'::jsonb ELSE projected.value END
         FROM (SELECT {expression} AS value OFFSET 0) projected)
    """


_REQUEST_ENTRY = f"""
    jsonb_build_object(
        'state', {_scalar("(entry.value -> 'state')", _TOKEN)},
        'aid_id', {_scalar("(entry.value -> 'aid_id')", _TOKEN)},
        'requested_at', {_scalar("(entry.value -> 'requested_at')", _DATE)},
        'completed_at', {_scalar("(entry.value -> 'completed_at')", _DATE)},
        'specialist_id', {_scalar("(entry.value -> 'specialist_id')", _INTEGER, numeric=True)},
        'completed_message_id',
            {_scalar("(entry.value -> 'completed_message_id')", _INTEGER, numeric=True)}
    )
"""
_FOLLOWUP_ENTRY = f"""
    jsonb_build_object(
        'state', {_scalar("(entry.value -> 'state')", _TOKEN)},
        'due_at', {_scalar("(entry.value -> 'due_at')", _DATE)},
        'sent_at', {_scalar("(entry.value -> 'sent_at')", _DATE)},
        'screen', {_scalar("(entry.value -> 'screen')", _TOKEN)},
        'context', CASE
            WHEN entry.value -> 'context' IS NULL
                 OR entry.value -> 'context' = 'null'::jsonb THEN NULL
            WHEN jsonb_typeof(entry.value -> 'context') = 'object' THEN jsonb_build_object(
                'aid_id', {_scalar("(entry.value #> '{context,aid_id}')", _TOKEN)},
                'request_key', {_scalar("(entry.value #> '{context,request_key}')", _TOKEN)}
            )
            ELSE '[]'::jsonb END
    )
"""
_REQUESTS_JSON = _bounded_json(
    _project_entries("(c.custom_attributes -> 'scenario_requests')", _REQUEST_ENTRY)
)
_FOLLOWUPS_JSON = _bounded_json(
    _project_entries("(c.custom_attributes -> 'scenario_followups')", _FOLLOWUP_ENTRY)
)
_QUEUE_EVENTS_SCOPE = """
    CASE WHEN sq.first_conversation_id IS NULL OR sq.last_conversation_id IS NULL THEN 'partial'
         WHEN events.all_valid IS TRUE AND events.key_count=1 THEN 'complete'
         WHEN events.all_valid IS TRUE THEN 'partial'
         ELSE 'invalid' END
"""
_QUEUE_EVENT_VALUES = _bounded_json(
    "CASE WHEN sq.first_conversation_id IS NOT NULL AND sq.last_conversation_id IS NOT NULL "
    "AND events.all_valid IS TRUE AND events.key_count=1 THEN events.valid_keys ELSE '[]'::jsonb END"
)
# Unlinked certificate inventory has no account attribution. Both the inventory
# and its otherwise global pool metadata are limited to links observed in scope.
_POOL_SCOPE = (
    f"WITH {_SCOPED_CONVERSATIONS}, {_SCOPED_MESSAGES}, {_SCOPED_CERTIFICATES}, "
    f"{_SCOPED_QUEUE}, {_SCOPED_POOLS}"
)
_TAG_SCOPE = (
    f"WITH {_SCOPED_CONVERSATIONS}, {_SCOPED_CONTACT_INBOXES}, {_SCOPED_CONTACTS}, "
    f"{_SCOPED_TAGGINGS}"
)

SELECTS = {
    "inboxes": """
        SELECT i.id, i.account_id, i.channel_type, i.csat_survey_enabled,
               i.lock_to_single_conversation, i.updated_at
        FROM public.inboxes i
        WHERE i.account_id = $1::bigint AND i.id = ANY($2::bigint[])
          AND i.id > $3::bigint
        ORDER BY i.id LIMIT $4::integer
    """,
    "contacts": f"""
        WITH {_SCOPED_CONVERSATIONS}, {_SCOPED_CONTACT_INBOXES}, {_SCOPED_CONTACTS}
        SELECT ct.id, ct.account_id, ct.created_at, ct.updated_at
        FROM public.contacts ct
        JOIN scoped_contacts sc ON sc.id = ct.id
        WHERE ct.id > $4::bigint
        ORDER BY ct.id LIMIT $5::integer
    """,
    "contact_inboxes": f"""
        WITH {_SCOPED_CONTACT_INBOXES}
        SELECT ci.id, ci.contact_id, ci.inbox_id, ci.created_at, ci.updated_at
        FROM public.contact_inboxes ci
        JOIN scoped_contact_inboxes sci ON sci.id = ci.id
        WHERE ci.id > $4::bigint
        ORDER BY ci.id LIMIT $5::integer
    """,
    "conversations": f"""
        WITH {_SCOPED_CONVERSATIONS}
        SELECT c.id, c.account_id, c.contact_id, c.inbox_id, c.contact_inbox_id,
               c.created_at, c.updated_at, c.status, c.status_changed_at, c.assignee_id,
               c.assignee_agent_bot_id, c.ai_assignee_type,
               {_REQUESTS_JSON} AS scenario_requests,
               {_FOLLOWUPS_JSON} AS scenario_followups
        FROM public.conversations c
        JOIN scoped_conversations sc ON sc.id = c.id
        WHERE c.id > $5::bigint
          AND ($4::timestamp IS NULL OR c.created_at >= $4 OR c.updated_at >= $4)
        ORDER BY c.id LIMIT $6::integer
    """,
    "messages": f"""
        WITH {_SCOPED_CONVERSATIONS}, {_SCOPED_MESSAGES}
        SELECT m.id, m.account_id, m.conversation_id, m.inbox_id, m.sender_id, m.sender_type,
               m.created_at, m.updated_at, m.message_type, m.private, m.status
        FROM public.messages m
        JOIN scoped_messages sm ON sm.id = m.id
        WHERE m.id > $5::bigint
          AND ($4::timestamp IS NULL OR m.created_at >= $4 OR m.updated_at >= $4)
        ORDER BY m.id LIMIT $6::integer
    """,
    "users": """
        SELECT u.id, u.type, u.updated_at
        FROM public.users u
        WHERE u.id > $2::bigint AND EXISTS (
            SELECT 1 FROM public.account_users au
            WHERE au.user_id = u.id AND au.account_id = $1::bigint
        )
        ORDER BY u.id LIMIT $3::integer
    """,
    "labels": """
        SELECT l.id, l.account_id, l.title, l.updated_at
        FROM public.labels l
        WHERE l.account_id = $1::bigint AND l.title = ANY($2::text[]) AND l.id > $3::bigint
        ORDER BY l.id LIMIT $4::integer
    """,
    "tags": f"""
        {_TAG_SCOPE}
        SELECT t.id, t.name
        FROM public.tags t
        WHERE t.id > $5::bigint AND t.name = ANY($4::text[])
          AND EXISTS (SELECT 1 FROM scoped_taggings st WHERE st.tag_id = t.id)
        ORDER BY t.id LIMIT $6::integer
    """,
    "taggings": f"""
        {_TAG_SCOPE}
        SELECT st.id, st.tag_id, st.taggable_type, st.taggable_id, st.context, st.created_at
        FROM scoped_taggings st
        WHERE st.id > $5::bigint
        ORDER BY st.id LIMIT $6::integer
    """,
    "csat_survey_responses": f"""
        WITH {_SCOPED_CONVERSATIONS}
        SELECT cs.id, cs.account_id, cs.conversation_id, cs.message_id, cs.contact_id,
               cs.rating, cs.created_at, cs.updated_at
        FROM public.csat_survey_responses cs
        JOIN scoped_conversations sc ON sc.id = cs.conversation_id AND sc.contact_id = cs.contact_id
        WHERE cs.account_id = $1::bigint AND cs.id > $4::bigint
        ORDER BY cs.id LIMIT $5::integer
    """,
    "women_help_certificate_pools": f"""
        {_POOL_SCOPE}
        SELECT p.id, p.provider
        FROM public.women_help_certificate_pools p
        JOIN scoped_pools sp ON sp.pool_id = p.id
        WHERE p.id > $4::bigint
        ORDER BY p.id LIMIT $5::integer
    """,
    "women_help_certificate_pool_aids": f"""
        {_POOL_SCOPE}
        SELECT pa.pool_id, pa.aid_id
        FROM public.women_help_certificate_pool_aids pa
        JOIN scoped_pools sp ON sp.pool_id = pa.pool_id
        WHERE (pa.pool_id, pa.aid_id) > ($4::bigint, $5::text)
        ORDER BY pa.pool_id, pa.aid_id LIMIT $6::integer
    """,
    "women_help_pdf_certificates": f"""
        WITH {_SCOPED_CONVERSATIONS}, {_SCOPED_MESSAGES}, {_SCOPED_CERTIFICATES}
        SELECT cert.id, $1::bigint AS account_id, cert.pool_id, cert.contact_id, cert.conversation_id,
               cert.source_message_id, cert.chatwoot_message_id, cert.nominal_rubles,
               cert.selected_aid_id, cert.status, cert.record_kind, cert.is_test, cert.valid_from,
               cert.expires_at, cert.reserved_at, cert.submitted_at, cert.delivered_at,
               cert.accepted_at, cert.issued_on,
               sc.resolved_contact_id, sc.resolved_conversation_id
        FROM public.women_help_pdf_certificates cert
        JOIN scoped_certificates sc ON sc.id = cert.id
        WHERE cert.id > $4::bigint
        ORDER BY cert.id LIMIT $5::integer
    """,
    "women_help_certificate_requests": f"""
        WITH {_SCOPED_CONVERSATIONS}, {_SCOPED_QUEUE}
        SELECT req.id, req.account_id, req.contact_id, sq.first_conversation_id AS conversation_id,
               CASE WHEN sq.first_conversation_id IS NOT NULL THEN req.source_message_id END
                   AS source_message_id,
               sq.last_conversation_id,
               CASE WHEN sq.last_conversation_id IS NOT NULL THEN req.last_source_message_id END
                   AS last_source_message_id,
               CASE WHEN sq.last_conversation_id IS NOT NULL THEN req.aid_id END AS aid_id,
               req.pool_id,
               CASE WHEN sq.last_conversation_id IS NOT NULL THEN req.reason END AS reason,
               CASE WHEN sq.last_conversation_id IS NOT NULL THEN req.status END AS status,
               CASE WHEN sq.first_conversation_id IS NOT NULL THEN req.first_requested_at END
                   AS first_requested_at,
               CASE WHEN sq.last_conversation_id IS NOT NULL THEN req.last_requested_at END
                   AS last_requested_at,
               CASE WHEN sq.last_conversation_id IS NOT NULL
                    THEN req.fulfilled_certificate_id END AS fulfilled_certificate_id,
               {_QUEUE_EVENTS_SCOPE} AS source_event_keys_scope,
               {_QUEUE_EVENT_VALUES} AS source_event_keys
        FROM public.women_help_certificate_requests req
        JOIN scoped_queue sq ON sq.id = req.id
        CROSS JOIN LATERAL (
            SELECT bool_and(e.valid) AS all_valid,
                   count(DISTINCT (e.value #>> '{{}}')) FILTER (WHERE e.valid) AS key_count,
                   jsonb_agg(e.value ORDER BY e.position) FILTER (WHERE e.valid) AS valid_keys
            FROM (
                SELECT event.value, event.position,
                       jsonb_typeof(event.value) = 'string'
                           AND (event.value #>> '{{}}') ~ '^{_TOKEN}$' AS valid
                FROM jsonb_array_elements(
                    CASE WHEN sq.first_conversation_id IS NOT NULL
                              AND sq.last_conversation_id IS NOT NULL
                              AND jsonb_typeof(req.source_event_keys) = 'array'
                         THEN req.source_event_keys ELSE '[]'::jsonb END
                ) WITH ORDINALITY AS event(value, position)
            ) e
        ) events
        WHERE req.id > $4::bigint
        ORDER BY req.id LIMIT $5::integer
    """,
}

_DELTA_TABLES = {"conversations", "messages"}
_LABEL_TABLES = {"labels", "tags", "taggings"}
_POOL_AIDS = "women_help_certificate_pool_aids"
_JSON_FIELDS = {
    "conversations": ("scenario_requests", "scenario_followups"),
    "women_help_certificate_requests": ("source_event_keys",),
}


async def read_snapshot(
    connection: Connection,
    scope: Scope,
    *,
    since: datetime | None = None,
    batch_size: int = 500,
) -> Snapshot:
    """Read an in-memory snapshot of up to 50,000 rows and 16 MiB of projected JSON.

    The server also caps each projected JSON value at 16 KiB. A limit breach
    aborts the snapshot. A naive cutoff is interpreted as UTC, like Chatwoot dates.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if since is not None and since.utcoffset() is not None:
        since = since.astimezone(UTC).replace(tzinfo=None)

    account = (scope.account_id,)
    limited_scope = (*account, scope.inbox_ids, scope.excluded_contact_ids)
    table_params = {
        "inboxes": (*account, scope.inbox_ids),
        "users": account,
        "labels": (*account, scope.label_titles),
        "tags": (*limited_scope, scope.label_titles),
        "taggings": (*limited_scope, scope.label_titles),
        "messages": (*limited_scope, since),
        "conversations": (*limited_scope, since),
    }
    tables: dict[str, list[dict]] = {table: [] for table in SELECTS}
    remaining = MAX_ROWS
    remaining_json_bytes = MAX_JSON_BYTES
    async with connection.transaction(isolation="repeatable_read", readonly=True):
        await connection.execute("SET LOCAL statement_timeout = '15s'")
        await connection.execute("SET LOCAL lock_timeout = '1s'")
        captured_at = await connection.fetchval("SELECT transaction_timestamp()")
        for table, sql in SELECTS.items():
            if table in _LABEL_TABLES and not scope.label_titles:
                continue
            params = table_params.get(table, limited_scope)
            cursor = (0, "") if table == _POOL_AIDS else (0,)
            while True:
                limit = min(batch_size, remaining + 1)
                records = await connection.fetch(sql, *params, *cursor, limit)
                remaining -= len(records)
                if remaining < 0:
                    raise RuntimeError("source snapshot row limit exceeded")
                for record in records:
                    row = dict(record)
                    for field in _JSON_FIELDS.get(table, ()):
                        value = row.get(field)
                        if value is None:
                            continue
                        payload = value if isinstance(value, str) else json.dumps(value)
                        if payload == "true":
                            raise RuntimeError("source snapshot JSON payload limit exceeded")
                        remaining_json_bytes -= len(payload.encode())
                        if remaining_json_bytes < 0:
                            raise RuntimeError("source snapshot JSON byte limit exceeded")
                        if isinstance(value, str):
                            row[field] = json.loads(value)
                    tables[table].append(row)
                if len(records) < limit:
                    break
                last = records[-1]
                cursor = (last["pool_id"], last["aid_id"]) if table == _POOL_AIDS else (last["id"],)

    complete_tables = set(tables) if since is None else set(tables) - _DELTA_TABLES
    return Snapshot(
        tables, complete_tables, {row["id"] for row in tables["conversations"]}, captured_at
    )

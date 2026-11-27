"""Pure, metadata-only projection. Does not import the application or open connections."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

AIDS = frozenset(
    (
        "food_card",
        "medicine_card",
        "hostel_3_nights",
        "children_card",
        "psychologist_3_sessions",
        "legal_consultation",
        "peer_consultation",
    )
)
SPECS = {
    "contacts": ("dim_contact", "contact_key", "contact", ("created_at", "updated_at")),
    "inboxes": (
        "dim_inbox",
        "inbox_key",
        "inbox",
        ("channel_type", "csat_survey_enabled", "lock_to_single_conversation", "updated_at"),
    ),
    "users": ("dim_staff", "staff_key", "staff", ("type", "updated_at")),
    "contact_inboxes": (
        "bridge_contact_inbox",
        "contact_inbox_key",
        "contact_inbox",
        ("contact_id", "inbox_id", "created_at", "updated_at"),
    ),
    "conversations": (
        "fact_conversation",
        "conversation_key",
        "conversation",
        (
            "contact_id",
            "inbox_id",
            "contact_inbox_id",
            "status",
            "status_changed_at",
            "assignee_id",
            "created_at",
            "updated_at",
            "assignee_agent_bot_id",
            "ai_assignee_type",
        ),
    ),
    "messages": (
        "fact_message",
        "message_key",
        "message",
        (
            "conversation_id",
            "inbox_id",
            "sender_type",
            "sender_id",
            "created_at",
            "updated_at",
            "message_type",
            "private",
            "status",
        ),
    ),
    "women_help_certificate_pools": ("dim_certificate_pool", "pool_key", "pool", ("provider",)),
    "women_help_pdf_certificates": (
        "fact_certificate",
        "certificate_key",
        "certificate",
        (
            "pool_id",
            "contact_id",
            "conversation_id",
            "source_message_id",
            "chatwoot_message_id",
            "nominal_rubles",
            "selected_aid_id",
            "status",
            "record_kind",
            "is_test",
            "valid_from",
            "expires_at",
            "reserved_at",
            "submitted_at",
            "delivered_at",
            "accepted_at",
            "issued_on",
        ),
    ),
    "women_help_certificate_requests": (
        "fact_certificate_request",
        "certificate_request_key",
        "certificate_request",
        (
            "contact_id",
            "conversation_id",
            "source_message_id",
            "last_conversation_id",
            "last_source_message_id",
            "aid_id",
            "pool_id",
            "reason",
            "status",
            "first_requested_at",
            "last_requested_at",
            "fulfilled_certificate_id",
        ),
    ),
    "labels": ("dim_label", "label_key", "label", ("title", "updated_at")),
    "tags": ("dim_tag", "tag_key", "tag", ("name",)),
    "csat_survey_responses": (
        "fact_csat",
        "csat_key",
        "csat",
        ("conversation_id", "message_id", "contact_id", "rating", "created_at", "updated_at"),
    ),
}
KEY_FIELDS = {
    "contact_id": ("contact_key", "contact"),
    "inbox_id": ("inbox_key", "inbox"),
    "conversation_id": ("conversation_key", "conversation"),
    "contact_inbox_id": ("contact_inbox_key", "contact_inbox"),
    "assignee_id": ("assignee_staff_key", "staff"),
    "assignee_agent_bot_id": ("assignee_bot_key", "bot"),
    "pool_id": ("pool_key", "pool"),
    "source_message_id": ("source_message_key", "message"),
    "chatwoot_message_id": ("send_message_key", "message"),
    "last_conversation_id": ("last_conversation_key", "conversation"),
    "last_source_message_id": ("last_source_message_key", "message"),
    "message_id": ("message_key", "message"),
    "fulfilled_certificate_id": ("fulfilled_certificate_key", "certificate"),
    "specialist_id": ("specialist_staff_key", "staff"),
    "completed_message_id": ("completed_message_key", "message"),
}
ENUMS = {
    ("fact_conversation", "status"): {0, 1, 2, 3},
    ("fact_message", "message_type"): {0, 1, 2, 3},
    ("fact_message", "status"): {0, 1, 2, 3},
    ("fact_message", "sender_type"): {"User", "Contact", "AgentBot"},
    ("dim_staff", "type"): {"User", "SuperAdmin"},
    ("fact_certificate", "record_kind"): {"certificate", "historical_issuance"},
    ("fact_certificate", "status"): {
        "available",
        "reserved",
        "submitted",
        "delivered",
        "failed",
        "accepted",
        "recorded",
    },
    ("fact_certificate_request", "status"): {"waiting", "fulfilled", "cancelled"},
    ("fact_certificate_request", "reason"): {"paused", "out_of_stock"},
}
TOKEN = re.compile(r"^[A-Za-z0-9_:.-]{1,256}$")


@dataclass(frozen=True)
class Issue:
    table: str
    entity_key: UUID | None
    code: str


@dataclass
class Projection:
    tables: dict[str, list[dict]]
    issues: list[Issue] = field(default_factory=list)


def entity_key(secret: bytes, account: int, kind: str, value: object) -> UUID:
    if len(secret) < 32:
        raise ValueError("DDS identity key must contain at least 32 bytes")
    payload = json.dumps(["dds-v1", account, kind, str(value)], separators=(",", ":")).encode()
    return UUID(bytes=hmac.digest(secret, payload, "sha256")[:16])


def row_hash(row: dict) -> str:
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def project(snapshot, scope, key: bytes) -> Projection:
    """Project selected source rows into independent facts and scoped shared dimensions."""
    # Validate identity material even for an empty source.
    entity_key(key, scope.account_id, "validation", 1)
    result = Projection({spec[0]: [] for spec in SPECS.values()})
    for name in (
        "fact_consultation_request",
        "fact_followup",
        "bridge_pool_aid",
        "bridge_certificate_request_event",
        "bridge_object_tag",
        "bridge_label_tag",
        "dim_aid",
    ):
        result.tables[name] = []

    def issue(table, ident, code):
        result.issues.append(Issue(table, ident, code))

    def pk(kind, value, table="", ident=None):
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            issue(table, ident, "invalid_identifier")
            return None
        return entity_key(key, scope.account_id, kind, value)

    def timestamp(value, table, ident, *, day=False):
        if value is None:
            return None
        try:
            if day:
                if type(value) is date:
                    return value
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                return date.fromisoformat(value)
            if isinstance(value, str) and len(value) <= 40:
                value = datetime.fromisoformat(value)
            if not isinstance(value, datetime):
                raise TypeError
            value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            if not 2000 <= value.year <= 2100:
                raise ValueError
            return value
        except ValueError, TypeError, OverflowError:
            issue(table, ident, "invalid_date")
            return None

    def scalar(field_name, value, table, ident):
        if value is None:
            return None
        if not isinstance(value, (str, int, bool, date, datetime)):
            issue(table, ident, "invalid_" + field_name)
            return None
        if field_name.endswith("_at") or field_name in ("valid_from", "expires_at"):
            return timestamp(value, table, ident)
        if field_name == "issued_on":
            return timestamp(value, table, ident, day=True)
        if field_name in ("aid_id", "selected_aid_id"):
            if isinstance(value, str) and value in AIDS:
                return value
        elif field_name in (
            "is_test",
            "private",
            "csat_survey_enabled",
            "lock_to_single_conversation",
        ):
            if type(value) is bool:
                return value
        elif (table, field_name) in ENUMS:
            options = ENUMS[table, field_name]
            if type(value) is type(next(iter(options))) and value in options:
                return value
        elif field_name == "rating":
            if type(value) is int and 1 <= value <= 5:
                return value
        elif field_name == "nominal_rubles":
            if type(value) is int and 0 <= value <= 2147483647:
                return value
        elif field_name == "channel_type":
            if value in {
                "Channel::Api",
                "Channel::Telegram",
                "Channel::WebWidget",
                "Channel::Email",
                "Channel::Whatsapp",
                "Channel::FacebookPage",
                "Channel::Line",
                "Channel::TwitterProfile",
                "Channel::Sms",
                "Channel::TwilioSms",
            }:
                return value
        elif field_name == "provider":
            if value in {"Ozon", "Пятёрочка", "Пятерочка", "ozon", "pyaterochka"}:
                return value
        elif field_name == "ai_assignee_type":
            if value in {"AgentBot", "Captain::Assistant", "CaptainAssistant"}:
                return value
        elif field_name in ("title", "name") and value in scope.label_titles:
            return value
        issue(table, ident, "invalid_" + field_name)
        return None

    def selected(source, row):
        if row.get("account_id", scope.account_id) != scope.account_id:
            return False
        if source == "contacts" and row.get("id") in scope.excluded_contact_ids:
            return False
        if row.get("contact_id") in scope.excluded_contact_ids:
            return False
        if source in ("inboxes", "conversations", "messages", "contact_inboxes"):
            inbox = row.get("id") if source == "inboxes" else row.get("inbox_id")
            if inbox is not None and inbox not in scope.inbox_ids:
                return False
        return True

    for source, (table, pkcol, kind, fields) in SPECS.items():
        for raw in snapshot.tables.get(source, []):
            if not selected(source, raw):
                continue
            if (
                source in ("labels", "tags")
                and raw.get("title" if source == "labels" else "name") not in scope.label_titles
            ):
                continue
            ident = pk(kind, raw.get("id"), table)
            if ident is None:
                issue(table, None, "missing_identifier")
                continue
            row = {pkcol: ident, "account_id": scope.account_id}
            for f in fields:
                if f in KEY_FIELDS:
                    col, target = KEY_FIELDS[f]
                    row[col] = pk(target, raw.get(f), table, ident)
                elif f == "sender_id":
                    sender_kind = {"User": "staff", "Contact": "contact", "AgentBot": "bot"}.get(
                        raw.get("sender_type") if isinstance(raw.get("sender_type"), str) else None
                    )
                    row["sender_key"] = (
                        pk(sender_kind, raw.get(f), table, ident) if sender_kind else None
                    )
                else:
                    row["account_type" if f == "type" else f] = scalar(f, raw.get(f), table, ident)
            if (
                table == "fact_conversation"
                and row.get("status_changed_at")
                and row.get("created_at")
                and row["status_changed_at"] < row["created_at"]
            ):
                issue(table, ident, "status_before_created")
            result.tables[table].append(row)

    conversations = {
        r["id"]: r
        for r in snapshot.tables.get("conversations", [])
        if selected("conversations", r) and type(r.get("id")) is int
    }
    requests = set()

    def obj(value, table, ident):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError, TypeError:
                value = None
        if value is None:
            return {}
        if not isinstance(value, dict):
            issue(table, ident, "invalid_json_shape")
            return {}
        return value

    for cid, conv in conversations.items():
        convkey = pk("conversation", cid)
        entries = obj(conv.get("scenario_requests"), "fact_consultation_request", convkey)
        for entry, raw in entries.items():
            ident = entity_key(key, scope.account_id, "consultation_request", [cid, entry])
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("state"), str)
                or raw.get("state") not in {"requested", "completed"}
            ):
                issue("fact_consultation_request", ident, "invalid_request_state")
                continue
            if not isinstance(entry, str) or not TOKEN.fullmatch(entry):
                issue("fact_consultation_request", ident, "invalid_entry_key")
                continue
            requests.add((cid, entry))
            table = "fact_consultation_request"
            row = {
                "account_id": scope.account_id,
                "request_key": ident,
                "conversation_key": convkey,
                "state": raw["state"],
                "aid_id": scalar("aid_id", raw.get("aid_id"), table, ident),
                "requested_at": timestamp(raw.get("requested_at"), table, ident),
                "completed_at": timestamp(raw.get("completed_at"), table, ident),
                "specialist_staff_key": pk("staff", raw.get("specialist_id"), table, ident),
                "completed_message_key": pk(
                    "message", raw.get("completed_message_id"), table, ident
                ),
            }
            result.tables[table].append(row)
        jobs = obj(conv.get("scenario_followups"), "fact_followup", convkey)
        for entry, raw in jobs.items():
            ident = entity_key(key, scope.account_id, "followup", [cid, entry])
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("state"), str)
                or raw.get("state") not in {"pending", "sent", "answered", "cancelled", "expired"}
            ):
                issue("fact_followup", ident, "invalid_followup_state")
                continue
            if not isinstance(entry, str) or not TOKEN.fullmatch(entry):
                issue("fact_followup", ident, "invalid_entry_key")
                continue
            ctx = obj(raw.get("context"), "fact_followup", ident)
            ref = ctx.get("request_key")
            valid_ref = isinstance(ref, str) and TOKEN.fullmatch(ref)
            matched = valid_ref and (cid, ref) in requests
            screen = raw.get("screen")
            if screen is not None and (
                not isinstance(screen, str) or not re.fullmatch(r"[si][a-z0-9]{1,10}", screen)
            ):
                issue("fact_followup", ident, "invalid_screen")
                screen = None
            result.tables["fact_followup"].append(
                {
                    "account_id": scope.account_id,
                    "followup_key": ident,
                    "conversation_key": convkey,
                    "state": raw["state"],
                    "due_at": timestamp(raw.get("due_at"), "fact_followup", ident),
                    "sent_at": timestamp(raw.get("sent_at"), "fact_followup", ident),
                    "screen_code": screen,
                    "aid_id": scalar("aid_id", ctx.get("aid_id"), "fact_followup", ident),
                    "request_key": entity_key(
                        key, scope.account_id, "consultation_request", [cid, ref]
                    )
                    if matched
                    else None,
                    "target_kind": "consultation"
                    if matched
                    else "unresolved"
                    if ref is not None
                    else "none",
                    "unresolved_target_key": entity_key(
                        key, scope.account_id, "unresolved_request", [cid, ref]
                    )
                    if valid_ref and not matched
                    else None,
                }
            )
            if ref is not None and not matched:
                issue("fact_followup", ident, "unresolved_target")

    msgs = {
        r["id"]: r
        for r in snapshot.tables.get("messages", [])
        if selected("messages", r) and type(r.get("id")) is int
    }
    certs = {
        pk("certificate", r["id"]): r
        for r in snapshot.tables.get("women_help_pdf_certificates", [])
        if selected("women_help_pdf_certificates", r) and type(r.get("id")) is int and r["id"] > 0
    }
    for row in result.tables["fact_certificate"]:
        raw = certs[row["certificate_key"]]
        message = msgs.get(raw.get("chatwoot_message_id"), {})
        cid = raw.get("resolved_conversation_id") or message.get("conversation_id")
        contact = raw.get("resolved_contact_id") or conversations.get(cid, {}).get("contact_id")
        direct = bool(row.get("contact_key") or row.get("conversation_key"))
        row["resolved_contact_key"] = row.get("contact_key") or pk("contact", contact)
        row["resolved_conversation_key"] = row.get("conversation_key") or pk("conversation", cid)
        row["link_origin"] = "direct" if direct else "send_message" if cid else "unresolved"

    for raw in snapshot.tables.get("women_help_certificate_pool_aids", []):
        pool = pk("pool", raw.get("pool_id"), "bridge_pool_aid")
        aid = scalar("aid_id", raw.get("aid_id"), "bridge_pool_aid", pool)
        if pool and aid:
            result.tables["bridge_pool_aid"].append(
                {"account_id": scope.account_id, "pool_key": pool, "aid_id": aid}
            )
    for raw in snapshot.tables.get("women_help_certificate_requests", []):
        if not selected("women_help_certificate_requests", raw):
            continue
        ident = pk("certificate_request", raw.get("id"))
        event_scope = raw.get("source_event_keys_scope", "complete")
        if event_scope != "complete":
            issue(
                "fact_certificate_request",
                ident,
                "partial_scope_events" if event_scope == "partial" else "invalid_event_list",
            )
            continue
        events = raw.get("source_event_keys") or []
        if isinstance(events, str):
            try:
                events = json.loads(events)
            except ValueError:
                events = None
        if not isinstance(events, list):
            issue("fact_certificate_request", ident, "invalid_event_list")
            continue
        for event in dict.fromkeys(x for x in events if isinstance(x, str) and TOKEN.fullmatch(x)):
            result.tables["bridge_certificate_request_event"].append(
                {
                    "account_id": scope.account_id,
                    "certificate_request_key": ident,
                    "event_key": entity_key(key, scope.account_id, "source_event", event),
                }
            )

    tags = {
        r["id"]: r
        for r in snapshot.tables.get("tags", [])
        if r.get("name") in scope.label_titles and selected("tags", r)
    }
    for raw in snapshot.tables.get("taggings", []):
        kind = {"Conversation": "conversation", "Contact": "contact"}.get(raw.get("taggable_type"))
        context = raw.get("context")
        if not kind or raw.get("tag_id") not in tags or not isinstance(context, str):
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", context):
            continue
        target = pk(kind, raw.get("taggable_id"))
        if target is None or not selected("taggings", raw):
            continue
        ident = entity_key(
            key, scope.account_id, "tagging", [kind, raw["taggable_id"], raw["tag_id"], context]
        )
        result.tables["bridge_object_tag"].append(
            {
                "account_id": scope.account_id,
                "tagging_key": ident,
                "tag_key": pk("tag", raw["tag_id"]),
                "taggable_type": raw["taggable_type"],
                "object_key": target,
                "context": context,
                "created_at": timestamp(raw.get("created_at"), "bridge_object_tag", ident),
            }
        )
    for label in result.tables["dim_label"]:
        ls = [r for r in result.tables["dim_label"] if r["title"] == label["title"]]
        ts = [r for r in result.tables["dim_tag"] if r["name"] == label["title"]]
        if len(ls) == len(ts) == 1:
            result.tables["bridge_label_tag"].append(
                {
                    "account_id": scope.account_id,
                    "label_key": label["label_key"],
                    "tag_key": ts[0]["tag_key"],
                }
            )
        elif ts:
            issue("bridge_label_tag", label["label_key"], "ambiguous_label_match")
    for aid in sorted(
        {
            r[f]
            for rows in result.tables.values()
            for r in rows
            for f in ("aid_id", "selected_aid_id")
            if r.get(f)
        }
    ):
        result.tables["dim_aid"].append({"account_id": scope.account_id, "aid_id": aid})

    from analytics.dds.catalog import load_catalog

    time_pairs = {
        "fact_consultation_request": [
            ("requested_at", "completed_at", "completed_before_requested")
        ],
        "fact_certificate": [("valid_from", "expires_at", "expiry_before_validity")],
        "fact_certificate_request": [
            ("first_requested_at", "last_requested_at", "last_before_first_request")
        ],
    }
    for table, rows in result.tables.items():
        for row in rows:
            ident = next(
                (v for k, v in row.items() if k.endswith("_key") and isinstance(v, UUID)), None
            )
            for first, last, code in time_pairs.get(table, []):
                if row.get(first) and row.get(last) and row[last] < row[first]:
                    issue(table, ident, code)
            if (
                row.get("created_at")
                and row.get("updated_at")
                and row["updated_at"] < row["created_at"]
            ):
                issue(table, ident, "updated_before_created")
            if table == "fact_consultation_request":
                if row.get("requested_at") is None:
                    issue(table, ident, "missing_requested_at")
                if row["state"] == "completed" and row.get("completed_at") is None:
                    issue(table, ident, "missing_completed_at")
            if (
                table == "fact_followup"
                and row["state"] in ("sent", "answered")
                and row.get("sent_at") is None
            ):
                issue(table, ident, "missing_sent_at")
    for table, definition in load_catalog()["tables"].items():
        fields = definition["columns"]
        unique = {}
        for row in result.tables.get(table, []):
            row = {c: row.get(c) for c in fields}
            ident = tuple(row.get(c) for c in definition["primary_key"])
            if any(x is None for x in ident):
                issue(table, None, "missing_primary_key")
                continue
            old = unique.get(ident)
            if old is not None and old != row:
                if table == "bridge_object_tag":
                    dates = [x for x in (old.get("created_at"), row.get("created_at")) if x]
                    row["created_at"] = min(dates) if dates else None
                else:
                    issue(
                        table,
                        next((x for x in ident if isinstance(x, UUID)), None),
                        "duplicate_conflict",
                    )
                    if (old.get("updated_at") or datetime.min.replace(tzinfo=UTC)) > (
                        row.get("updated_at") or datetime.min.replace(tzinfo=UTC)
                    ):
                        row = old
            unique[ident] = row
        result.tables[table] = list(unique.values())
    return result

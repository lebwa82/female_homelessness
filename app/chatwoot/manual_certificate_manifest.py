"""Pure, fail-closed plans for historical manual-certificate surveys.

This module deliberately has no database, Chatwoot, inventory or filesystem API.
A pending job in a manifest is a proposal, never an activated follow-up.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

ACCOUNT_ID = 1
MSK = ZoneInfo("Europe/Moscow")
KNOWN_AIDS = frozenset({"food_card", "children_card", "medicine_card", "hostel_3_nights"})
KNOWN_PROVIDERS = frozenset({"ozon", "pyaterochka"})
REQUIRED_CAPABILITIES = (
    "historical_certificate_survey_copy",
    "recheck_existing_certificate_followups",
)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")  # noqa: TRY004 - one validation contract
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("source_message_created_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("source_message_created_at must be an ISO timestamp") from None
    # The source query returns naive PostgreSQL UTC timestamps, never local wall time.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _source(value: Mapping[str, Any]) -> dict:
    spreadsheet_id = value.get("spreadsheet_id")
    if (not isinstance(spreadsheet_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id)):
        raise ValueError("source spreadsheet_id must be a Google Sheets identifier")
    if value.get("sheet_name") != "Sheet1" or value.get("gid") != 0:
        raise ValueError("source must identify Sheet1 with gid 0")
    if isinstance(value.get("gid"), bool) or not isinstance(value.get("gid"), int):
        raise ValueError("source gid must be integer 0")  # noqa: TRY004 - one validation contract
    digest = value.get("snapshot_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("source snapshot_sha256 must be a SHA-256 hexadecimal digest")
    return {"spreadsheet_id": spreadsheet_id, "sheet_name": "Sheet1", "gid": 0,
            "snapshot_sha256": digest.lower()}


def build_followup_manifest(
    rows: Iterable[Mapping[str, Any]], source: Mapping[str, Any], *,
    generated_at: datetime, expected_count: int = 39,
) -> dict:
    """Validate normalized evidence and return an inert, deterministic survey plan.

    The sheet's issuance day is authoritative. Source-message time is retained
    only as evidence and does not substitute for the sheet date. Historical aid
    and provider are validated independently: today's pool mapping is not proof
    that a historical combination was invalid.
    """
    if (not isinstance(generated_at, datetime) or generated_at.tzinfo is None
            or generated_at.utcoffset() is None):
        raise ValueError("generated_at must be a timezone-aware datetime")
    _positive_int(expected_count, "expected_count")
    normalized_source = _source(source)
    input_rows = list(rows)
    if len(input_rows) != expected_count:
        raise ValueError("source row count does not match expected_count")
    seen = {field: set() for field in (
        "source_row", "conversation_id", "internal_conversation_id", "contact_id",
        "source_message_id", "job_key",
    )}
    jobs = []
    for row in input_rows:
        ids = {field: _positive_int(row.get(field), field) for field in (
            "source_row", "conversation_id", "internal_conversation_id", "contact_id",
            "source_message_id",
        )}
        if ids["source_row"] < 2:
            raise ValueError("source_row must reference a data row after the header")
        key = f"certificate:manual:{ACCOUNT_ID}:{ids['contact_id']}"
        for field, value in {**ids, "job_key": key}.items():
            if value in seen[field]:
                raise ValueError(f"duplicate {field}")
            seen[field].add(value)
        aid_id, provider_slug = row.get("aid_id"), row.get("provider_slug")
        if not isinstance(aid_id, str) or aid_id not in KNOWN_AIDS:
            raise ValueError("unknown historical aid_id")
        if not isinstance(provider_slug, str) or provider_slug not in KNOWN_PROVIDERS:
            raise ValueError("unknown historical provider_slug")
        issued_on = row.get("issued_on")
        if not isinstance(issued_on, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", issued_on):
            raise ValueError("issued_on must be a YYYY-MM-DD date")
        try:
            issued_date = date.fromisoformat(issued_on)
        except ValueError:
            raise ValueError("issued_on must be a valid date") from None
        if issued_date >= generated_at.astimezone(MSK).date():
            raise ValueError("issued_on must be a past date in Europe/Moscow")
        accepted = _boolean(row.get("telegram_acceptance_verified"),
                            "telegram_acceptance_verified")
        human_assigned = _boolean(row.get("human_assigned"), "human_assigned")
        message_time = _timestamp(row.get("source_message_created_at"))
        if message_time > generated_at:
            raise ValueError("source_message_created_at cannot be in the future")
        due = datetime.combine(issued_date + timedelta(days=4), time(10), tzinfo=MSK)
        if due > generated_at:
            raise ValueError("historical survey due_at cannot be in the future")
        spreadsheet_id = quote(normalized_source["spreadsheet_id"], safe="")
        sheet_row = ids["source_row"]
        jobs.append({
            "job_key": key, "account_id": ACCOUNT_ID, **ids,
            "source_date": issued_on, "aid_id": aid_id, "provider_slug": provider_slug,
            "telegram_acceptance_verified": accepted,
            "source_message_created_at": message_time.isoformat(),
            "currently_human_assigned": human_assigned,
            "source_ref": f"sheet:{normalized_source['spreadsheet_id']}:0:{sheet_row}",
            "source_reference": {
                "spreadsheet_id": normalized_source["spreadsheet_id"],
                "sheet_name": "Sheet1", "gid": 0, "row": sheet_row,
                "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
                       f"#gid=0&range=A{sheet_row}",
            },
            "payload": {"screen": "s5", "state": "pending", "due_at": due.isoformat(),
                        "context": {"aid_id": aid_id, "historical_survey": True}},
        })
    jobs.sort(key=lambda job: job["source_row"])
    return {
        "apply": False, "schema_version": 1, "account_id": ACCOUNT_ID,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "source": normalized_source, "required_capabilities": list(REQUIRED_CAPABILITIES),
        "jobs": jobs,
    }

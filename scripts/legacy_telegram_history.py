"""Read a pg_dump COPY export as data, never execute its SQL or print its contents."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

SOURCE = "nevidimy-manual-telegram"
COPY = re.compile(r"COPY public\.(\w+) \(([^)]+)\) FROM stdin;\n?$")
REQUIRED = {
    "users": {"id", "tg_id", "state", "created_at", "updated_at"},
    "messages": {"id", "user_id", "direction", "text", "created_at"},
}


class ImportConflict(ValueError):
    """Safe error: the message must not contain source data."""


def copy_value(value: str) -> str | None:
    if value == r"\N":
        return None
    escapes = {b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r",
               b"t": b"\t", b"v": b"\v"}

    def replace(match):
        token = match[1]
        if token[:1] in b"01234567":
            return bytes([int(token, 8)])
        if token.startswith(b"x") and len(token) > 1:
            return bytes([int(token[1:], 16)])
        return escapes.get(token, token)

    return re.sub(rb"\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|.)", replace,
                  value.encode("utf-8")).decode("utf-8")


def read_copy(path: Path) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    current, columns, size = None, [], 0
    with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
        for line in source:
            size += len(line)
            if size > 50_000_000:
                raise ImportConflict("export_too_large")
            if current is not None:
                if line.rstrip("\r\n") == r"\.":
                    current = None
                    continue
                if current in REQUIRED:
                    values = line.rstrip("\r\n").split("\t")
                    if len(values) != len(columns):
                        raise ImportConflict("invalid_copy_row")
                    tables[current].append(dict(zip(columns, map(copy_value, values))))
                continue
            if line.startswith("COPY "):
                match = COPY.fullmatch(line)
                if not match:
                    raise ImportConflict("unsupported_copy_header")
                current, column_text = match.groups()
                columns = column_text.split(", ")
                if current in REQUIRED:
                    if current in tables or not REQUIRED[current].issubset(columns):
                        raise ImportConflict("invalid_copy_schema")
                    if len(set(columns)) != len(columns):
                        raise ImportConflict("duplicate_copy_column")
                    tables[current] = []
    if current is not None or set(tables) != set(REQUIRED):
        raise ImportConflict("incomplete_export")
    return tables


def positive_id(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ImportConflict("invalid_id")
    result = int(value)
    if not 0 < result < 2**63:
        raise ImportConflict("invalid_id")
    return result


def timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ImportConflict("timezone_required")
    return parsed.isoformat()


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def load_history(path: Path) -> dict:
    tables = read_copy(path)
    users, telegram_ids = {}, set()
    for row in tables["users"]:
        user_id, tg_id = positive_id(row["id"]), positive_id(row["tg_id"])
        if user_id in users or tg_id in telegram_ids:
            raise ImportConflict("duplicate_user")
        telegram_ids.add(tg_id)
        if not isinstance(row["state"], str) or not row["state"]:
            raise ImportConflict("missing_legacy_state")
        users[user_id] = {"id": user_id, "telegram_id": str(tg_id),
                          "finished": row["state"] == "CLOSE",
                          "legacy_state": row["state"],
                          "created_at": timestamp(row["created_at"]),
                          "updated_at": timestamp(row["updated_at"]), "messages": []}
    seen = set()
    for row in tables["messages"]:
        message_id, user_id = positive_id(row["id"]), positive_id(row["user_id"])
        if message_id in seen or user_id not in users:
            raise ImportConflict("duplicate_or_orphan_message")
        seen.add(message_id)
        direction = row["direction"]
        private = isinstance(direction, str) and re.fullmatch(r"notify:[0-9]+", direction)
        if direction not in {"in", "out"} and not private:
            raise ImportConflict("unknown_message_direction")
        if not isinstance(row["text"], str) or "\x00" in row["text"]:
            raise ImportConflict("invalid_message_text")
        message = {"id": message_id, "content": row["text"], "private": bool(private),
                   "direction": "incoming" if direction == "in" else "outgoing",
                   "created_at": timestamp(row["created_at"])}
        message["sha256"] = fingerprint({**message, "user_id": user_id})
        users[user_id]["messages"].append(message)
    if not users or not seen:
        raise ImportConflict("empty_export")
    for user in users.values():
        user["messages"].sort(key=lambda m: (datetime.fromisoformat(m["created_at"]), m["id"]))
    return {"format": 1, "source": SOURCE, "users": sorted(users.values(), key=lambda u: u["id"])}


def summary(history: dict) -> dict:
    messages = [m for user in history["users"] for m in user["messages"]]
    types = Counter("private_note" if m["private"] else m["direction"] for m in messages)
    dates = [datetime.fromisoformat(m["created_at"]) for m in messages]
    return {"users": len(history["users"]), "messages": len(messages), **types,
            "unfinished": sum(not u["finished"] for u in history["users"]),
            "first_message_at": min(dates).isoformat(), "last_message_at": max(dates).isoformat()}

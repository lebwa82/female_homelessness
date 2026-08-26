"""Read non-secret metadata for the release currently serving the bot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
RELEASE_METADATA_FILENAME = ".release.json"


@dataclass(frozen=True)
class ReleaseInfo:
    revision: str | None
    released_at: str


def active_release_info() -> ReleaseInfo:
    """Return the active release revision and activation time without raising.

    The file is created only after systemd has accepted the new bot process.
    A local checkout and legacy release deliberately fall back to ``unknown``.
    """

    metadata_path = Path(__file__).resolve().parents[1] / RELEASE_METADATA_FILENAME
    try:
        raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        revision = raw_metadata.get("revision")
        released_at_raw = raw_metadata.get("released_at_utc")
        if not isinstance(revision, str) or not revision.strip():
            revision = None
        if not isinstance(released_at_raw, str):
            return ReleaseInfo(revision=revision, released_at="неизвестно")
        released_at = datetime.fromisoformat(released_at_raw)
        if released_at.tzinfo is None:
            return ReleaseInfo(revision=revision, released_at="неизвестно")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ReleaseInfo(revision=None, released_at="неизвестно")

    return ReleaseInfo(
        revision=revision,
        released_at=released_at.astimezone(MOSCOW_TIMEZONE).strftime("%d.%m.%Y %H:%M MSK"),
    )

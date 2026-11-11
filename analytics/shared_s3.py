"""The scope of the one previously issued team dashboard and its workbook."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import parse_qs, urlsplit

_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_SIG_FIELDS = {
    "X-Amz-Algorithm",
    "X-Amz-Credential",
    "X-Amz-Date",
    "X-Amz-Expires",
    "X-Amz-SignedHeaders",
    "X-Amz-Signature",
}


@dataclass(frozen=True)
class TeamShare:
    identifier: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not re.fullmatch(_UUID, self.identifier) or self.expires_at.utcoffset() is None:
            raise ValueError("Invalid analytics team share scope")

    def key(self, filename: str) -> str:
        if filename not in {"dashboard.html", "latest.xlsx"}:
            raise ValueError("Unknown analytics team share object")
        return f"team-share/{self.identifier}/{filename}"

    def active(self) -> bool:
        return datetime.now(UTC) < self.expires_at


def parse_signed_share(url: str, bucket: str, filename: str) -> TeamShare:
    """Validate a key-scoped SigV4 URL, without ever logging the credential."""
    parts = urlsplit(url)
    match = re.fullmatch(
        rf"/{re.escape(bucket)}/team-share/({_UUID})/{re.escape(filename)}", parts.path
    )
    params = parse_qs(parts.query)
    if (parts.scheme, parts.netloc, parts.fragment) != (
        "https",
        "s3.twcstorage.ru",
        "",
    ) or not match:
        raise ValueError("Unexpected analytics signed link target")
    if set(params) != _SIG_FIELDS or any(len(values) != 1 for values in params.values()):
        raise ValueError("Unexpected analytics signed link scope")
    if (
        params["X-Amz-Algorithm"] != ["AWS4-HMAC-SHA256"]
        or params["X-Amz-SignedHeaders"] != ["host"]
        or not re.fullmatch(r"[0-9a-f]{64}", params["X-Amz-Signature"][0])
        or not re.fullmatch(r"[1-9][0-9]{0,5}", params["X-Amz-Expires"][0])
    ):
        raise ValueError("Invalid analytics signed link parameters")
    duration = int(params["X-Amz-Expires"][0])
    if duration > 604800:
        raise ValueError("Unexpected analytics signature duration")
    date = datetime.strptime(params["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return TeamShare(match[1], date + timedelta(seconds=duration))


def workbook_link(html: str) -> str:
    links = re.findall(r'<a id="download-excel"[^>]*\bhref="([^"<>]+)"', html)
    if len(links) != 1:
        raise ValueError("Expected one Excel button in the shared dashboard")
    return unescape(links[0])

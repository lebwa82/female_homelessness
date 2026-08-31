"""Small, defensive contracts for Agent Bot events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IncomingChatwootMessage:
    """The only event shape that can start an automated reply."""

    message_id: int
    conversation_id: int
    contact_id: int
    inbox_id: int
    content: str


def parse_message_created(payload: object) -> IncomingChatwootMessage | None:
    """Return a public inbound text message, otherwise intentionally ignore it."""
    if not isinstance(payload, dict) or payload.get("event") != "message_created":
        return None
    if payload.get("message_type") != "incoming" or payload.get("private") is True:
        return None

    conversation = payload.get("conversation")
    sender = payload.get("sender")
    inbox = payload.get("inbox")
    content = payload.get("content")
    if not isinstance(conversation, dict) or not isinstance(sender, dict) or not isinstance(inbox, dict):
        return None
    if not isinstance(content, str) or not content.strip():
        return None

    message_id = _positive_int(payload.get("id"))
    conversation_id = _positive_int(conversation.get("id"))
    contact_id = _positive_int(sender.get("id"))
    inbox_id = _positive_int(inbox.get("id"))
    if None in {message_id, conversation_id, contact_id, inbox_id}:
        return None
    return IncomingChatwootMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        inbox_id=inbox_id,
        content=content.strip(),
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

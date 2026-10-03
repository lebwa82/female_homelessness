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


@dataclass(frozen=True, slots=True)
class ConversationChanged:
    conversation_id: int


RETURN_TO_BOT = "[women-help:return-to-bot]"
CONSULTATION_COMPLETED = "[women-help:consultation-completed]"


@dataclass(frozen=True, slots=True)
class StaffMessage:
    message_id: int
    conversation_id: int
    sender_id: int
    return_to_bot: bool = False
    consultation_completed: bool = False
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class MessageDeliveryChanged:
    message_id: int
    conversation_id: int
    status: str


def parse_message_delivery_changed(payload: object) -> MessageDeliveryChanged | None:
    if not isinstance(payload, dict) or payload.get("event") != "message_updated":
        return None
    if payload.get("message_type") != "outgoing":
        return None
    status = payload.get("status")
    conversation = payload.get("conversation")
    if status not in {"sent", "delivered", "read", "failed"} or not isinstance(
        conversation, dict
    ):
        return None
    message_id = _positive_int(payload.get("id"))
    conversation_id = _positive_int(conversation.get("id"))
    if message_id is None or conversation_id is None:
        return None
    return MessageDeliveryChanged(message_id, conversation_id, status)


def parse_staff_message(payload: object) -> StaffMessage | None:
    """Only authenticated Chatwoot User messages may control bot ownership."""
    if not isinstance(payload, dict) or payload.get("event") != "message_created":
        return None
    if payload.get("message_type") != "outgoing":
        return None
    sender, conversation = payload.get("sender"), payload.get("conversation")
    if not isinstance(sender, dict) or sender.get("type") != "user":
        return None
    if not isinstance(conversation, dict):
        return None
    returning = payload.get("private") is True and payload.get("content") == RETURN_TO_BOT
    content = payload.get("content") or ""
    completed = payload.get("private") is True and content == CONSULTATION_COMPLETED
    request_id = None
    prefix = CONSULTATION_COMPLETED[:-1] + ":"
    if (payload.get("private") is True and isinstance(content, str)
            and content.startswith(prefix) and content.endswith("]")):
        candidate = content[len(prefix):-1]
        if len(candidate) == 24 and all(c in "0123456789abcdef" for c in candidate):
            completed, request_id = True, candidate
    if payload.get("private") is True and not (returning or completed):
        return None
    ids = [_positive_int(v) for v in (payload.get("id"), conversation.get("id"), sender.get("id"))]
    if None in ids:
        return None
    return StaffMessage(*ids, return_to_bot=returning,
                        consultation_completed=completed, request_id=request_id)


def parse_conversation_changed(payload: object) -> ConversationChanged | None:
    if not isinstance(payload, dict) or payload.get("event") not in {
        "conversation_updated",
        "conversation_status_changed",
    }:
        return None
    conversation_id = _positive_int(payload.get("id"))
    return ConversationChanged(conversation_id) if conversation_id else None


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
    if (
        not isinstance(conversation, dict)
        or not isinstance(sender, dict)
        or not isinstance(inbox, dict)
    ):
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
    except TypeError, ValueError:
        return None
    return parsed if parsed > 0 else None

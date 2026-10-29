"""Small, defensive contracts for Agent Bot events."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.chatwoot.fast_buttons import CAPABILITY, PRIVATE_NOTES_FEATURE

BUTTON_SNAPSHOT_MAX_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class IncomingChatwootMessage:
    """The only event shape that can start an automated reply."""

    message_id: int
    conversation_id: int
    contact_id: int
    inbox_id: int
    content: str
    # Present only after webhook HMAC verification. A proposal, never truth.
    button_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConversationChanged:
    conversation_id: int


_BOT_BOOKKEEPING_ATTRIBUTES = frozenset({
    "workflow_state",
    "workflow_need",
    "pending_aid_id",
    "pending_contact_method",
    "pending_city",
    "pending_district",
    "pending_offer",
    "workflow_navigation",
    "scenario",
    "scenario_pending_input",
    "scenario_last_message_id",
    "scenario_event_receipts",
    "scenario_followups",
    "bot_last_diagnostics",
    "bot_last_decision",
})


RETURN_TO_BOT = "[women-help:return-to-bot]"
CONSULTATION_COMPLETED = "[women-help:consultation-completed]"
CONSULTATION_SCHEDULE = "[women-help:consultation-schedule]"
CONSULTATION_CANCEL = "[women-help:consultation-cancel]"
CONSULTATION_SUCCESS = {
    "[women-help:consultation-success:psychologist_3_sessions]": "psychologist_3_sessions",
    "[women-help:consultation-success:legal_consultation]": "legal_consultation",
}


@dataclass(frozen=True, slots=True)
class StaffMessage:
    message_id: int
    conversation_id: int
    sender_id: int
    return_to_bot: bool = False
    consultation_completed: bool = False
    request_id: str | None = None
    consultation_schedule: str | None = None
    consultation_aid_id: str | None = None
    occurred_at: str | None = None


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
    # "sent" has no service-side effect; ignore it before durable enqueue/locks.
    if status not in {"delivered", "read", "failed"} or not isinstance(
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
    aid_id = (CONSULTATION_SUCCESS.get(content)
              if payload.get("private") is True and isinstance(content, str) else None)
    completed = completed or aid_id is not None
    scheduling = ({CONSULTATION_SCHEDULE: "schedule", CONSULTATION_CANCEL: "cancel"}.get(content)
                  if payload.get("private") is True and isinstance(content, str) else None)
    request_id = None
    prefix = CONSULTATION_COMPLETED[:-1] + ":"
    if (payload.get("private") is True and isinstance(content, str)
            and content.startswith(prefix) and content.endswith("]")):
        candidate = content[len(prefix):-1]
        if len(candidate) == 24 and all(c in "0123456789abcdef" for c in candidate):
            completed, request_id = True, candidate
    if payload.get("private") is True and not (returning or completed or scheduling):
        return None
    ids = [_positive_int(v) for v in (payload.get("id"), conversation.get("id"), sender.get("id"))]
    if None in ids:
        return None
    return StaffMessage(*ids, return_to_bot=returning,
                        consultation_completed=completed, request_id=request_id,
                        consultation_schedule=scheduling, consultation_aid_id=aid_id,
                        occurred_at=_staff_event_time(payload.get("created_at")) if aid_id else None)


def _staff_event_time(value: object) -> str | None:
    """Use the private macro message time, not its delayed processing time."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.isoformat() if stamp.tzinfo is not None else None
    except ValueError:
        return None


def parse_conversation_changed(payload: object) -> ConversationChanged | None:
    if not isinstance(payload, dict) or payload.get("event") not in {
        "conversation_updated",
        "conversation_status_changed",
    }:
        return None
    if payload["event"] == "conversation_updated" and _only_bot_bookkeeping_changed(payload):
        return None
    conversation_id = _positive_int(payload.get("id"))
    return ConversationChanged(conversation_id) if conversation_id else None


def _only_bot_bookkeeping_changed(payload: dict[str, Any]) -> bool:
    """Ignore only proven workflow writes, never ownership/routing or unknown deltas."""
    changes = payload.get("changed_attributes")
    if not isinstance(changes, list) or not changes:
        return False
    attributes = None
    seen = set()
    # Native BaseListener serializes previous_changes as single-key objects,
    # with complete old/new custom-attribute hashes rather than a partial patch.
    for item in changes:
        if not isinstance(item, dict) or len(item) != 1:
            return False
        name, delta = next(iter(item.items()))
        if name not in {"custom_attributes", "updated_at"} or name in seen:
            return False
        seen.add(name)
        if not isinstance(delta, dict) or set(delta) != {"previous_value", "current_value"}:
            return False
        if name == "custom_attributes":
            attributes = delta
        else:
            try:
                if not all(
                    isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
                    for value in delta.values()
                ):
                    return False
            except ValueError:
                return False
    if attributes is None:
        return False
    previous, current = attributes["previous_value"], attributes["current_value"]
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    keys = previous.keys() | current.keys()
    if not all(isinstance(key, str) for key in keys):
        return False
    try:
        previous_values = {
            key: json.dumps(value, sort_keys=True, allow_nan=False)
            for key, value in previous.items()
        }
        current_values = {
            key: json.dumps(value, sort_keys=True, allow_nan=False)
            for key, value in current.items()
        }
        changed_keys = {
            key for key in keys
            if previous_values.get(key) != current_values.get(key)
        }
    except (TypeError, ValueError):
        return False
    return bool(changed_keys) and changed_keys <= _BOT_BOOKKEEPING_ATTRIBUTES


def parse_message_created(
    payload: object, *, trusted_snapshot: bool = False,
) -> IncomingChatwootMessage | None:
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

    # The timeline shows the clicked button's label; only the Telegram adapter
    # supplies the original action for routing. Native webhooks expose the channel
    # on conversation, not inbox; retain the older inbox hint for compatibility.
    attributes = payload.get("content_attributes")
    channel = conversation.get("channel", inbox.get("channel_type"))
    if (channel == "Channel::Telegram" and isinstance(attributes, dict)
            and "nevidimy_telegram_callback_data" in attributes):
        callback = attributes["nevidimy_telegram_callback_data"]
        if not isinstance(callback, str) or not callback.strip():
            return None
        try:
            if len(callback.encode()) > 64:
                return None
        except UnicodeEncodeError:
            return None
        content = callback

    message_id = _positive_int(payload.get("id"))
    conversation_id = _positive_int(conversation.get("id"))
    contact_id = _positive_int(sender.get("id"))
    inbox_id = _positive_int(inbox.get("id"))
    if None in {message_id, conversation_id, contact_id, inbox_id}:
        return None
    event = IncomingChatwootMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        inbox_id=inbox_id,
        content=content.strip(),
    )
    if (trusted_snapshot and channel == "Channel::Telegram" and isinstance(attributes, dict)
            and "nevidimy_telegram_callback_data" in attributes):
        return replace(event, button_snapshot=validated_button_snapshot(
            payload.get("nevidimy_button_snapshot"), event,
        ))
    return event


def validated_button_snapshot(value: object, event: IncomingChatwootMessage) -> dict | None:
    """Validate the signed extension without discarding its actionable event."""
    if not isinstance(value, dict) or value.keys() != {
        "schema", "capability", "features", "input_id", "contact_id", "inbox_id",
        "version", "conversation",
    }:
        return None
    if (type(value["schema"]) is not int or value["schema"] != 1
            or value["capability"] != CAPABILITY or value["features"] != [PRIVATE_NOTES_FEATURE]):
        return None
    for name, expected in (("input_id", event.message_id), ("contact_id", event.contact_id),
                           ("inbox_id", event.inbox_id)):
        if type(value[name]) is not int or value[name] != expected:
            return None
    version, conversation = value["version"], value["conversation"]
    if not isinstance(version, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", version,
    ) is None:
        return None
    if not isinstance(conversation, dict) or conversation.keys() != {
        "id", "status", "custom_attributes", "assignee_id", "team_id", "meta",
    }:
        return None
    if (type(conversation["id"]) is not int or conversation["id"] != event.conversation_id
            or conversation["status"] not in ("open", "pending", "resolved", "snoozed")
            or not isinstance(conversation["custom_attributes"], dict)):
        return None
    for name in ("assignee_id", "team_id"):
        item = conversation[name]
        if item is not None and (type(item) is not int or item <= 0):
            return None
    assignee, team = conversation["assignee_id"], conversation["team_id"]
    if conversation["meta"] != {
        "assignee": {"id": assignee} if assignee else None,
        "assignee_type": "User" if assignee else "AgentBot",
        "team": {"id": team} if team else None,
    }:
        return None
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size > BUTTON_SNAPSHOT_MAX_BYTES:
            return None
        return deepcopy(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return None
    return parsed if parsed > 0 else None

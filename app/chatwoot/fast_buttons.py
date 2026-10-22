"""Speculative button planning; the native Chatwoot transaction is authoritative.

This is an API buffer, not another scenario engine or durable state store. A plan
can change approved scenario state, buffer private notes and create one plain
reply. Every external capability fails closed before anything leaves this process.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

BUTTON_ATTRIBUTES = frozenset(
    {
        "workflow_state",
        "workflow_need",
        "pending_aid_id",
        "pending_contact_method",
        "pending_city",
        "pending_district",
        "pending_offer",
        "context_epoch",
        "workflow_navigation",
        "scenario",
        "scenario_pending_input",
        "scenario_last_message_id",
        "scenario_followups",
        "scenario_requests",
        "scenario_event_receipts",
        "consultation_reviewed",
    }
)
CAPABILITY = "nevidimy-atomic-turn-v1"
PRIVATE_NOTES_FEATURE = "private_notes_v1"


class UnsupportedFastButton(Exception):
    """No live effects occurred; the established handler can safely take over."""


def validate_button_notes(notes: list[dict[str, str]]) -> None:
    """Match the bounded native private-note contract before any HTTP write."""
    if not isinstance(notes, list) or len(notes) > 4:
        raise UnsupportedFastButton("unsupported_private_notes")
    total = 0
    for note in notes:
        if not isinstance(note, dict) or note.keys() != {"content", "event_key"}:
            raise UnsupportedFastButton("unsupported_private_notes")
        content, key = note["content"], note["event_key"]
        if not isinstance(content, str) or not isinstance(key, str):
            raise UnsupportedFastButton("unsupported_private_notes")
        try:
            size = len(content.encode("utf-8"))
            key_size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            raise UnsupportedFastButton("unsupported_private_notes") from None
        if (
            size > 32_768
            or not 0 < key_size <= 256
            or re.fullmatch(r"scenario(?:-alert)?:[0-9a-f]{24}", key) is None
        ):
            raise UnsupportedFastButton("unsupported_private_notes")
        total += size
    if total > 65_536:
        raise UnsupportedFastButton("unsupported_private_notes")


class BufferedButtonApi:
    def __init__(self, conversation: dict[str, Any]) -> None:
        self._initial = copy.deepcopy(conversation)
        self._conversation = copy.deepcopy(conversation)
        self._reply: dict[str, Any] | None = None
        self._notes: list[dict[str, str]] = []

    def _check(self, conversation_id: int) -> None:
        if type(conversation_id) is not int or conversation_id != self._conversation["id"]:
            raise UnsupportedFastButton("conversation_mismatch")

    @property
    def attributes(self) -> dict[str, Any]:
        initial = self._initial.get("custom_attributes") or {}
        current = self._conversation.get("custom_attributes") or {}
        return copy.deepcopy(
            {
                key: value
                for key, value in current.items()
                if key not in initial
                or json.dumps(value, sort_keys=True, allow_nan=False)
                != json.dumps(initial[key], sort_keys=True, allow_nan=False)
            }
        )

    @property
    def reply(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._reply)

    @property
    def notes(self) -> list[dict[str, str]]:
        return copy.deepcopy(self._notes)

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        self._check(conversation_id)
        return copy.deepcopy(self._conversation)

    async def get_messages(self, conversation_id: int, *, after_id=None) -> tuple:
        self._check(conversation_id)
        return ()

    async def has_reply_for_turn(self, conversation_id: int, turn_key: str, **kwargs) -> bool:
        self._check(conversation_id)
        # The read checked existing receipts; the commit repeats this in the DB.
        return False

    async def set_custom_attributes(self, conversation_id: int, attributes: dict) -> None:
        self._check(conversation_id)
        if not isinstance(attributes, dict) or not attributes.keys() <= BUTTON_ATTRIBUTES:
            raise UnsupportedFastButton("unsupported_attributes")
        self._conversation["custom_attributes"] = {
            **(self._conversation.get("custom_attributes") or {}),
            **copy.deepcopy(attributes),
        }

    async def add_private_note(self, conversation_id: int, content: str, *, event_key=None) -> None:
        self._check(conversation_id)
        note = {"content": content, "event_key": event_key}
        validate_button_notes([note])
        if any(existing["event_key"] == event_key for existing in self._notes):
            return
        updated = [*self._notes, note]
        validate_button_notes(updated)
        self._notes = updated

    async def send_reply(
        self,
        conversation_id: int,
        *,
        text,
        choices,
        turn_key,
        sensitive_content=None,
        attachment=None,
    ) -> None:
        self._check(conversation_id)
        if attachment is not None or self._reply is not None:
            raise UnsupportedFastButton("unsupported_reply")
        self._reply = copy.deepcopy(
            {
                "text": text,
                "choices": choices,
                "turn_key": turn_key,
                "sensitive_content": sensitive_content,
            }
        )

    def __getattr__(self, name):
        async def unavailable(*args, **kwargs):
            raise UnsupportedFastButton("external_effect")

        return unavailable


async def no_external_effect(*args, **kwargs):
    raise UnsupportedFastButton("external_effect")


class NoExternalGateway:
    def __getattr__(self, name):
        return no_external_effect

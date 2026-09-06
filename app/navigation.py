"""Append-only dialogue checkpoints; navigation never reverts business effects."""

from __future__ import annotations

from typing import Any

WORKFLOW_FIELDS = (
    "state", "need", "pending_aid_id", "pending_contact_method",
    "pending_city", "pending_district", "pending_offer",
)


def checkpoint_update(record: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Include navigation in the same atomic write as a workflow transition."""
    if "navigation" in values:
        return values
    before = {name: getattr(record, name) for name in WORKFLOW_FIELDS}
    after = {name: values.get(name, value) for name, value in before.items()}
    reset = values.get("context_epoch", record.context_epoch) != record.context_epoch
    journal = record.navigation
    entries = list(journal.get("entries", []))
    cursor = journal.get("cursor")
    changed_elsewhere = cursor is not None and entries[cursor]["workflow"] != before
    if before == after and not reset and not changed_elsewhere:
        return values
    if reset:
        # Keep the old checkpoints, but /clear starts a disconnected navigation root.
        cursor = None
    elif cursor is None or changed_elsewhere:
        # A follow-up worker can advance the row outside ConversationStore.
        entries.append({"workflow": before, "previous": cursor})
        cursor = len(entries) - 1
    elif before["state"] == after["state"] and before["need"] == after["need"]:
        # Draft-field updates refine the current state, not an extra user-facing step.
        cursor = entries[cursor]["previous"]
    if before != after or reset:
        entries.append({"workflow": after, "previous": cursor})
        cursor = len(entries) - 1
    return {
        **values,
        "navigation": {
            "entries": entries,
            "cursor": cursor,
            "revision": journal.get("revision", 0) + 1,
        },
    }


def previous_checkpoint(navigation: dict[str, Any]) -> int | None:
    cursor = navigation.get("cursor")
    if cursor is None:
        return None
    return navigation["entries"][cursor]["previous"]


def back_update(navigation: dict[str, Any], callback_id: str) -> dict[str, Any] | None:
    """Reject stale buttons even when Telegram assigns a new callback query ID."""
    if callback_id != f"back:{navigation.get('revision', 0)}":
        return None
    previous = previous_checkpoint(navigation)
    if previous is None:
        return None
    return {
        **navigation["entries"][previous]["workflow"],
        "navigation": {
            **navigation,
            "cursor": previous,
            "revision": navigation["revision"] + 1,
        },
    }

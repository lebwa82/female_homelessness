"""Safe, deterministic ordering for the staged production release gate."""

from __future__ import annotations


def staged_release_gate(revision: str, staging_dir: str, target_dir: str) -> tuple[str, ...]:
    """Return the mandatory release steps without exposing environment values.

    The arguments make the staged/activation boundary explicit for callers while
    keeping this helper entirely offline and testable.
    """
    if not revision or not staging_dir or not target_dir:
        raise ValueError("release metadata is required")
    return (
        "just check",
        "just scenario-smoke",
        "just eval-dialogues",
        "just db-assure",
        "activate-release",
    )

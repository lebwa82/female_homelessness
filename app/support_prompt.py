"""Bounded, atomically updated conversation style text shared by local processes."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

MAX_PROMPT_BYTES = 16_384
_MAX_STATE_BYTES = MAX_PROMPT_BYTES * 4 + 4096
_STATE_NAME = "support_prompt.json"
_LOCK_NAME = ".support_prompt.lock"


class PromptStoreError(Exception):
    """A safe error message that never includes submitted text or filesystem paths."""


class PromptConflictError(PromptStoreError):
    """The version confirmed by the operator is no longer active."""


@dataclass(frozen=True, slots=True)
class PromptSnapshot:
    text: str | None
    revision: str
    previous_revision: str | None


@dataclass(frozen=True, slots=True)
class _Record:
    text: str | None = None
    revision: str = "builtin"
    actor_id: int | None = None
    created_at: str | None = None


def validate_prompt_bytes(data: bytes) -> str:
    if not isinstance(data, bytes) or len(data) > MAX_PROMPT_BYTES:
        raise PromptStoreError("The prompt must be a UTF-8 text file of at most 16 KiB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PromptStoreError("The prompt must contain valid UTF-8 text.") from None
    if not text.strip():
        raise PromptStoreError("The prompt must not be empty.")
    if any(
        char not in "\t\r\n" and unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in text
    ):
        raise PromptStoreError("The prompt contains unsupported control characters.")
    return text


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _record(value: object) -> _Record:
    if not isinstance(value, dict) or set(value) != {"text", "revision", "actor_id", "created_at"}:
        raise ValueError
    record = _Record(**value)
    if record.revision == "builtin":
        if record != _Record():
            raise ValueError
        return record
    if not isinstance(record.revision, str) or UUID(record.revision).hex != record.revision:
        raise ValueError
    if type(record.actor_id) is not int or not 0 < record.actor_id < 2**63:
        raise ValueError
    if not isinstance(record.created_at, str) or len(record.created_at) > 40:
        raise ValueError
    if datetime.fromisoformat(record.created_at).utcoffset() != UTC.utcoffset(None):
        raise ValueError
    if record.text is not None:
        if not isinstance(record.text, str):
            raise ValueError
        if validate_prompt_bytes(record.text.encode("utf-8")) != record.text:
            raise ValueError
    return record


def _snapshot(active: _Record, previous: _Record | None) -> PromptSnapshot:
    return PromptSnapshot(active.text, active.revision, previous.revision if previous else None)


class PromptStore:
    """Store two versions in one fixed-name file in a trusted, private directory.

    Readers need only read access and always see a complete state. Writers use a
    separate advisory lock because replacing the state file changes its inode.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def load(self) -> PromptSnapshot:
        return _snapshot(*self._read())

    def _read(self) -> tuple[_Record, _Record | None]:
        try:
            fd = os.open(self.directory / _STATE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return _Record(), None
        except OSError:
            raise PromptStoreError("Could not read the prompt state.") from None
        try:
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError
                data = stream.read(_MAX_STATE_BYTES + 1)
            if len(data) > _MAX_STATE_BYTES:
                raise ValueError
            state = json.loads(data, object_pairs_hook=_unique_object)
            if not isinstance(state, dict) or set(state) != {
                "schema_version",
                "active",
                "previous",
            }:
                raise ValueError
            if type(state["schema_version"]) is not int or state["schema_version"] != 1:
                raise ValueError
            active = _record(state["active"])
            previous = _record(state["previous"]) if state["previous"] is not None else None
            if (active.revision == "builtin") != (previous is None):
                raise ValueError
            if previous and previous.revision == active.revision:
                raise ValueError
            return active, previous
        except OSError, ValueError, TypeError, RecursionError, PromptStoreError:
            raise PromptStoreError("The prompt state is invalid or unreadable.") from None

    def publish(self, text: str, actor_id: int, expected_revision: str) -> PromptSnapshot:
        try:
            validated = validate_prompt_bytes(text.encode("utf-8"))
        except AttributeError, UnicodeEncodeError:
            raise PromptStoreError("The prompt must contain valid UTF-8 text.") from None
        return self._change(validated, actor_id, expected_revision, rollback=False)

    def rollback(self, actor_id: int, expected_revision: str) -> PromptSnapshot:
        return self._change(None, actor_id, expected_revision, rollback=True)

    def _change(
        self, text: str | None, actor_id: int, expected_revision: str, *, rollback: bool
    ) -> PromptSnapshot:
        if type(actor_id) is not int or not 0 < actor_id < 2**63:
            raise PromptStoreError("A valid operator ID is required.")
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                self.directory / _LOCK_NAME,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            with os.fdopen(fd, "rb") as lock:
                if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                    raise PromptStoreError("The prompt lock is invalid.")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                active, previous = self._read()
                if active.revision != expected_revision:
                    raise PromptConflictError("The active prompt changed. Review it again.")
                if rollback:
                    if previous is None:
                        raise PromptStoreError("No previous prompt version is available.")
                    text = previous.text
                updated = _Record(text, uuid4().hex, actor_id, datetime.now(UTC).isoformat())
                self._write(updated, active)
                return _snapshot(updated, active)
        except OSError:
            raise PromptStoreError("Could not save the prompt state.") from None

    def _write(self, active: _Record, previous: _Record) -> None:
        data = json.dumps(
            {"schema_version": 1, "active": asdict(active), "previous": asdict(previous)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        fd, path = tempfile.mkstemp(prefix=".support_prompt-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.directory / _STATE_NAME)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

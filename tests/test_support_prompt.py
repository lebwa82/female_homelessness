import json
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest

from app.support_prompt import (
    MAX_PROMPT_BYTES,
    PromptConflictError,
    PromptSnapshot,
    PromptStore,
    PromptStoreError,
    validate_prompt_bytes,
)


@pytest.mark.parametrize("data", [b"hello", "Короткий текст".encode(), b"\xef\xbb\xbfhello"])
def test_validate_utf8_and_optional_bom(data):
    assert validate_prompt_bytes(data) == data.decode("utf-8-sig")


def test_validation_preserves_formatting_and_accepts_size_boundary():
    assert validate_prompt_bytes(b"  first\r\n\tsecond\n") == "  first\r\n\tsecond\n"
    assert len(validate_prompt_bytes(b"a" * MAX_PROMPT_BYTES)) == MAX_PROMPT_BYTES


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b" \t\r\n",
        b"\xef\xbb\xbf",
        b"\xff",
        b"\xff\xfeh\x00",
        b"text\x00",
        b"text\x01",
        b"text\x7f",
        "text\u0085".encode(),
        "text\u202e".encode(),
        "text\ufeff".encode(),
        b"a" * (MAX_PROMPT_BYTES + 1),
        "я".encode() * MAX_PROMPT_BYTES,
    ],
    ids=[
        "empty",
        "whitespace",
        "bom-only",
        "invalid-utf8",
        "utf16",
        "nul",
        "c0",
        "delete",
        "c1",
        "bidi",
        "embedded-bom",
        "oversize",
        "multibyte-oversize",
    ],
)
def test_reject_invalid_prompt(data):
    with pytest.raises(PromptStoreError):
        validate_prompt_bytes(data)


def test_validation_errors_never_echo_text():
    with pytest.raises(PromptStoreError) as error:
        validate_prompt_bytes(b"private-marker\x00")
    assert "private-marker" not in str(error.value)


def test_missing_store_uses_builtin_without_creating_directory(tmp_path):
    directory = tmp_path / "not-created"
    assert PromptStore(directory).load() == PromptSnapshot(None, "builtin", None)
    assert not directory.exists()


def test_publish_persists_audit_and_readers_reload(tmp_path):
    directory = tmp_path / "prompts"
    writer = PromptStore(directory)
    reader = PromptStore(directory)
    assert reader.load().revision == "builtin"
    first = writer.publish("First style", actor_id=123, expected_revision="builtin")
    assert first.text == "First style"
    assert first.previous_revision == "builtin"
    assert UUID(first.revision).hex == first.revision
    assert reader.load() == first
    second = writer.publish("Second style", actor_id=456, expected_revision=first.revision)
    assert second.revision != first.revision
    assert second.previous_revision == first.revision
    assert reader.load() == second
    state = json.loads((directory / "support_prompt.json").read_bytes())
    assert state["schema_version"] == 1
    assert state["active"]["actor_id"] == 456
    assert state["previous"]["actor_id"] == 123
    for record in (state["active"], state["previous"]):
        assert datetime.fromisoformat(record["created_at"]).tzinfo == UTC
    for name in ("support_prompt.json", ".support_prompt.lock"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_rollback_creates_new_revision_and_can_restore_builtin(tmp_path):
    store = PromptStore(tmp_path)
    first = store.publish("First style", 1, "builtin")
    second = store.publish("Second style", 2, first.revision)
    restored = store.rollback(3, second.revision)
    assert restored.text == first.text
    assert restored.revision not in {first.revision, second.revision}
    assert restored.previous_revision == second.revision
    assert store.load() == restored
    audit = json.loads((tmp_path / "support_prompt.json").read_bytes())
    assert audit["active"]["actor_id"] == 3
    assert store.rollback(4, restored.revision).text == "Second style"

    other = PromptStore(tmp_path / "other")
    initial = other.publish("Initial style", 1, "builtin")
    builtin = other.rollback(2, initial.revision)
    assert builtin.text is None
    assert builtin.revision not in {"builtin", initial.revision}
    assert builtin.previous_revision == initial.revision
    assert other.load() == builtin
    assert other.rollback(3, builtin.revision).text == initial.text


def test_rollback_without_previous_version_does_not_create_state(tmp_path):
    store = PromptStore(tmp_path)
    with pytest.raises(PromptStoreError, match="No previous"):
        store.rollback(1, "builtin")
    assert store.load().revision == "builtin"
    assert not (tmp_path / "support_prompt.json").exists()


@pytest.mark.parametrize("operation", ["publish", "rollback"])
def test_stale_confirmation_cannot_change_state(tmp_path, operation):
    store = PromptStore(tmp_path)
    current = store.publish("Current style", 1, "builtin")
    original = (tmp_path / "support_prompt.json").read_bytes()
    with pytest.raises(PromptConflictError):
        if operation == "publish":
            store.publish("Stale style", 2, "builtin")
        else:
            store.rollback(2, "builtin")
    assert store.load() == current
    assert (tmp_path / "support_prompt.json").read_bytes() == original


def test_concurrent_writers_accept_only_one_confirmation(tmp_path):
    barrier = Barrier(8)

    def publish(number):
        barrier.wait(timeout=5)
        try:
            return PromptStore(tmp_path).publish(f"Style {number}", number + 1, "builtin")
        except PromptConflictError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(8)))
    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert PromptStore(tmp_path).load() == successful[0]


def _publish_in_process(directory, number, start, results):
    if not start.wait(timeout=10):
        raise TimeoutError("The concurrent writers did not start.")
    try:
        snapshot = PromptStore(directory).publish(f"Style {number}", number + 1, "builtin")
    except PromptConflictError:
        results.put(None)
    else:
        results.put(snapshot.revision)


def test_separate_processes_accept_only_one_confirmation(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_publish_in_process, args=(tmp_path, number, start, results))
        for number in range(4)
    ]
    try:
        for worker in workers:
            worker.start()
        start.set()
        revisions = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        results.close()
        results.join_thread()
    successful = [revision for revision in revisions if revision is not None]
    assert len(successful) == 1
    assert PromptStore(tmp_path).load().revision == successful[0]


@pytest.mark.parametrize("failed_operation", ["replace", "fsync"])
def test_failure_before_replace_preserves_active_and_previous(
    tmp_path, monkeypatch, failed_operation
):
    store = PromptStore(tmp_path)
    first = store.publish("First style", 1, "builtin")
    second = store.publish("Second style", 2, first.revision)
    original = (tmp_path / "support_prompt.json").read_bytes()

    def fail(*args):
        raise OSError("private-filesystem-details")

    monkeypatch.setattr(os, failed_operation, fail)
    with pytest.raises(PromptStoreError) as error:
        store.publish("Third style", 3, second.revision)
    assert "private-filesystem-details" not in str(error.value)
    assert store.load() == second
    assert (tmp_path / "support_prompt.json").read_bytes() == original
    assert not list(tmp_path.glob(".support_prompt-*.tmp"))


def test_failure_after_replace_keeps_a_complete_recoverable_state(tmp_path, monkeypatch):
    store = PromptStore(tmp_path)
    initial = store.publish("First style", 1, "builtin")
    fsync = os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("private-filesystem-details")
        fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory_sync)
        with pytest.raises(PromptStoreError) as error:
            store.publish("Second style", 2, initial.revision)
    assert "private-filesystem-details" not in str(error.value)
    current = store.load()
    assert current.text == "Second style"
    assert current.previous_revision == initial.revision
    assert not list(tmp_path.glob(".support_prompt-*.tmp"))
    with pytest.raises(PromptConflictError):
        store.publish("Stale style", 3, initial.revision)
    assert store.rollback(3, current.revision).text == initial.text


def test_atomic_replace_exposes_only_complete_versions(tmp_path, monkeypatch):
    store = PromptStore(tmp_path)
    initial = store.publish("First style", 1, "builtin")
    replace = os.replace
    observations = []

    def observe_replace(source, destination):
        observations.append(store.load())
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        replace(source, destination)
        observations.append(store.load())

    monkeypatch.setattr(os, "replace", observe_replace)
    updated = store.publish("Second style", 2, initial.revision)
    assert observations == [initial, updated]


@pytest.mark.parametrize("data", [b"", b"not-json", b"{}", b"[]", b"x" * 100_000])
def test_invalid_persisted_state_fails_closed(tmp_path, data):
    path = tmp_path / "support_prompt.json"
    path.write_bytes(data)
    store = PromptStore(tmp_path)
    with pytest.raises(PromptStoreError):
        store.load()
    with pytest.raises(PromptStoreError):
        store.publish("New style", 1, "builtin")
    assert path.read_bytes() == data


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", ""),
        ("text", "x" * (MAX_PROMPT_BYTES + 1)),
        ("text", "bad\x00"),
        ("text", 17),
        ("revision", "not-a-revision"),
        ("revision", "builtin"),
        ("actor_id", True),
        ("actor_id", -1),
        ("created_at", "2026-01-01T00:00:00"),
        ("created_at", None),
    ],
    ids=[
        "empty",
        "oversize",
        "control",
        "type",
        "revision",
        "builtin",
        "bool-actor",
        "negative-actor",
        "naive-time",
        "missing-time",
    ],
)
def test_invalid_persisted_record_fails_closed(tmp_path, field, value):
    store = PromptStore(tmp_path)
    store.publish("Valid style", 1, "builtin")
    path = tmp_path / "support_prompt.json"
    state = json.loads(path.read_bytes())
    state["active"][field] = value
    path.write_text(json.dumps(state))
    with pytest.raises(PromptStoreError):
        store.load()


def test_duplicate_json_keys_and_invalid_previous_are_rejected(tmp_path):
    store = PromptStore(tmp_path)
    store.publish("Valid style", 1, "builtin")
    path = tmp_path / "support_prompt.json"
    data = path.read_text()
    path.write_text(data.replace('"schema_version":1', '"schema_version":1,"schema_version":1'))
    with pytest.raises(PromptStoreError):
        store.load()
    state = json.loads(data)
    state["previous"] = state["active"]
    path.write_text(json.dumps(state))
    with pytest.raises(PromptStoreError):
        store.load()


@pytest.mark.parametrize("name", ["support_prompt.json", ".support_prompt.lock"])
def test_store_rejects_symlinks_without_touching_target(tmp_path, name):
    external = tmp_path / "external"
    external.write_text("untouched")
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / name).symlink_to(external)
    with pytest.raises(PromptStoreError):
        PromptStore(directory).publish("New style", 1, "builtin")
    assert external.read_text() == "untouched"


@pytest.mark.parametrize("name", ["support_prompt.json", ".support_prompt.lock"])
def test_store_rejects_non_regular_files(tmp_path, name):
    (tmp_path / name).mkdir()
    with pytest.raises(PromptStoreError):
        PromptStore(tmp_path).publish("New style", 1, "builtin")


@pytest.mark.parametrize("text", ["", "x" * (MAX_PROMPT_BYTES + 1), "bad\x00", "bad\ud800"])
def test_publish_validates_input_even_without_upload(tmp_path, text):
    directory = tmp_path / "not-created"
    with pytest.raises(PromptStoreError):
        PromptStore(directory).publish(text, 1, "builtin")
    assert not directory.exists()


@pytest.mark.parametrize("actor_id", [0, -1, True, "1", 2**63])
def test_publish_rejects_invalid_operator(tmp_path, actor_id):
    with pytest.raises(PromptStoreError):
        PromptStore(tmp_path).publish("Valid style", actor_id, "builtin")
    assert not (tmp_path / "support_prompt.json").exists()


def test_maximum_escaped_text_round_trips_in_both_versions(tmp_path):
    store = PromptStore(tmp_path)
    text = "\\" * MAX_PROMPT_BYTES
    first = store.publish(text, 1, "builtin")
    second = store.publish(text, 2, first.revision)
    assert store.load() == second
    assert store.rollback(3, second.revision).text == text

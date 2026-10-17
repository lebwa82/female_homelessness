import gzip

import pytest

from scripts.legacy_telegram_history import ImportConflict, copy_value, load_history, summary


def dump(tmp_path, *, user_rows=None, messages=None, suffix=""):
    path = tmp_path / "history.sql.gz"
    user_rows = user_rows if user_rows is not None else [
        "1\t501\tHUMAN\t2026-10-01 10:00:00+03\t2026-10-04 12:00:00+03",
        "2\t502\tCLOSE\t2026-10-02 10:00:00+03\t2026-10-04 12:00:00+03",
    ]
    messages = messages if messages is not None else [
        "1\t1\tin\tПример\\nтекста\\tс вкладкой\\\\N\t2026-10-01 10:01:00+03",
        "2\t1\tout\tОтвет\t2026-10-01 10:02:00+03",
        "3\t1\tnotify:901\tСлужебное сообщение\t2026-10-01 10:03:00+03",
        "4\t2\tin\t[кнопка] Да\t2026-10-02 10:01:00+03",
    ]
    text = ("-- SQL outside COPY is never executed\nDROP DATABASE production;\n"
            "COPY public.users (id, tg_id, state, created_at, updated_at) FROM stdin;\n"
            + "\n".join(user_rows) + "\n\\.\n"
            + "COPY public.messages (id, user_id, direction, text, created_at) FROM stdin;\n"
            + "\n".join(messages) + "\n\\.\n" + suffix)
    with gzip.open(path, "wt") as out:
        out.write(text)
    return path


def test_text_only_history_preserves_direction_time_and_private_notes(tmp_path):
    history = load_history(dump(tmp_path))
    assert summary(history) == {
        "users": 2, "messages": 4, "incoming": 2, "outgoing": 1, "private_note": 1,
        "unfinished": 1, "first_message_at": "2026-10-01T10:01:00+03:00",
        "last_message_at": "2026-10-02T10:01:00+03:00",
    }
    first, closed = history["users"]
    assert not first["finished"] and closed["finished"]
    assert first["messages"][0]["content"] == "Пример\nтекста\tс вкладкой\\N"
    assert closed["messages"][0]["content"] == "[кнопка] Да"
    assert first["messages"][2]["private"]
    assert all("buttons" not in m and "callback_data" not in m for u in history["users"]
               for m in u["messages"])


@pytest.mark.parametrize(("encoded", "decoded"), [
    (r"\N", None), (r"\\N", r"\N"), (r"a\tb\nc\rd", "a\tb\nc\rd"),
    (r"\101\x42", "AB"), (r"\320\237\321\200", "Пр"),
    (r"\b\f\v", "\b\f\v"), ("Обычный текст", "Обычный текст"),
])
def test_postgres_copy_decoding(encoded, decoded):
    assert copy_value(encoded) == decoded


@pytest.mark.parametrize("field,value", [("id", "-1"), ("id", str(2**63)),
                                       ("id", "1.0"), ("user_id", "90"),
                                       ("direction", "unexpected"), ("text", r"\N"),
                                       ("text", r"\000"),
                                       ("created_at", "2026-10-01 10:00:00")])
def test_invalid_rows_fail_closed_without_returning_content(tmp_path, field, value):
    row = {"id": "1", "user_id": "1", "direction": "in", "text": "confidential sample",
           "created_at": "2026-10-01 10:01:00+03"}
    row[field] = value
    with pytest.raises((ImportConflict, ValueError)) as error:
        load_history(dump(tmp_path, messages=["\t".join(row.values())]))
    assert "confidential" not in str(error.value)


def test_duplicate_messages_and_users_rejected(tmp_path):
    row = "1\t1\tin\thello\t2026-10-01 10:01:00+03"
    with pytest.raises(ImportConflict, match="duplicate_or_orphan"):
        load_history(dump(tmp_path, messages=[row, row]))
    u = "1\t501\tHUMAN\t2026-10-01 10:00:00+03\t2026-10-04 12:00:00+03"
    with pytest.raises(ImportConflict, match="duplicate_user"):
        load_history(dump(tmp_path, user_rows=[u, u.replace("1\t501", "2\t501")]))


def test_incomplete_copy_and_unsupported_schema_rejected(tmp_path):
    with pytest.raises(ImportConflict, match="incomplete_export"):
        load_history(dump(tmp_path, suffix="COPY public.anything (id) FROM stdin;\n1\n"))
    with pytest.raises(ImportConflict, match="invalid_copy_schema"):
        load_history(dump(tmp_path, suffix="COPY public.users (id) FROM stdin;\n\\.\n"))


def test_ignore_legacy_followups_not_rearm_timers(tmp_path):
    history = load_history(dump(tmp_path, suffix=(
        "COPY public.followups (id, user_id, deliver_at) FROM stdin;\n"
        "1\t1\t2026-10-01 10:00:00+03\n\\.\n")))
    assert set(history) == {"format", "source", "users"}
    assert all("scenario_followups" not in user for user in history["users"])


def test_fingerprint_stable_but_changes_with_text(tmp_path):
    first = load_history(dump(tmp_path))
    assert load_history(dump(tmp_path)) == first
    changed = load_history(dump(tmp_path, messages=[
        "1\t1\tin\tchanged\t2026-10-01 10:01:00+03"]))
    assert first["users"][0]["messages"][0]["sha256"] != changed["users"][0]["messages"][0]["sha256"]

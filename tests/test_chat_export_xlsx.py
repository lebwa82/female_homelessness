from __future__ import annotations

import re
import stat
from datetime import UTC, datetime, timedelta, timezone
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from analytics import chat_export_xlsx
from analytics.chat_export_xlsx import ExportTable, write_workbook

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_CHECKED_AT = datetime(2026, 11, 11, 12, 0, tzinfo=timezone(timedelta(hours=3)))


async def _rows(*rows):
    for row in rows:
        yield row


def _sheet_cells(sheet):
    return {cell.attrib["r"]: cell for cell in sheet.findall(".//m:c", _NS)}


def _inline_string(cell):
    assert cell.get("t") == "inlineStr"
    escaped = "".join(t.text or "" for t in cell.findall("m:is//m:t", _NS))
    return re.sub(r"_x([0-9A-Fa-f]{4})_", lambda match: chr(int(match[1], 16)), escaped)


@pytest.mark.asyncio
async def test_xlsx_roundtrip_preserves_schema_values_and_literal_strings(tmp_path):
    path = tmp_path / "snapshot.xlsx"
    created_at = "2026-11-11 12:00:00.123456+03"
    sent_at = "2026-11-11 09:00:00.123456"
    synthetic_xml = "<r><t>demo</t></r></is><f>1+1</f><is><r><t>demo</t></r>"
    messages = (
        "=1+1",
        "{=1+1}",
        "https://example.invalid/demo",
        synthetic_xml,
        " Пример\r\nвторая строка\r_x000D_\n ",
        "",
        None,
        "x" * (32767 - 253) + "\n" * 253,
    )
    payload = '{"list": [1, null], "literal": "<r>тест</r>"}'
    conversations = ExportTable(
        "conversations",
        (
            ("id", "int8"),
            ("active", "bool"),
            ("status", "int4"),
            ("created_at", "timestamptz"),
            ("additional_attributes", "jsonb"),
            ("large_number", "int8"),
        ),
        _rows(("-9223372036854775808", "true", "0", created_at, payload, "1234567890123456")),
    )
    message_table = ExportTable(
        "messages",
        (("conversation_id", "int8"), ("content", "text"), ("sent_at", "timestamp")),
        _rows(*[("-9223372036854775808", content, sent_at) for content in messages]),
    )

    assert await write_workbook(path, [conversations, message_table], _CHECKED_AT) == {
        "conversations": 1,
        "messages": len(messages),
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]

    with ZipFile(path) as archive:
        assert archive.testzip() is None
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        assert [sheet.get("name") for sheet in book.findall(".//m:sheet", _NS)] == [
            "conversations",
            "messages",
        ]
        assert "xl/sharedStrings.xml" not in archive.namelist()
        assert not any(name.startswith("xl/externalLinks/") for name in archive.namelist())
        assert not any(name.startswith("xl/worksheets/_rels/") for name in archive.namelist())
        for name in archive.namelist():
            if name.endswith(".rels"):
                assert all(
                    node.get("TargetMode") != "External"
                    for node in ElementTree.fromstring(archive.read(name))
                )
        assert b"2026-11-11T09:00:00+00:00" in archive.read("docProps/custom.xml")
        sheets = [
            ElementTree.fromstring(archive.read(f"xl/worksheets/sheet{i}.xml")) for i in (1, 2)
        ]

    for sheet, expected_range in zip(sheets, ("A1:H2", f"A1:E{len(messages) + 1}"), strict=True):
        assert sheet.find(".//m:f", _NS) is None
        assert sheet.find(".//m:hyperlink", _NS) is None
        assert sheet.find("m:autoFilter", _NS).attrib["ref"] == expected_range
        pane = sheet.find(".//m:pane", _NS)
        assert pane.attrib["topLeftCell"] == "B2"
        assert pane.attrib["state"] == "frozen"

    cells = _sheet_cells(sheets[0])
    assert [_inline_string(cells[f"{col}1"]) for col in "ABCDEFGH"] == [
        "id",
        "active",
        "status",
        "created_at",
        "additional_attributes",
        "large_number",
        "__null_fields",
        "created_at__excel_utc",
    ]
    assert _inline_string(cells["A2"]) == "-9223372036854775808"
    assert cells["B2"].get("t") == "b"
    assert cells["B2"].findtext("m:v", namespaces=_NS) == "1"
    assert cells["C2"].get("t") is None
    assert cells["C2"].findtext("m:v", namespaces=_NS) == "0"
    assert _inline_string(cells["D2"]) == created_at
    assert _inline_string(cells["E2"]) == payload
    assert _inline_string(cells["F2"]) == "1234567890123456"
    assert _inline_string(cells["G2"]) == "[]"
    expected_serial = (
        datetime(2026, 11, 11, 9, 0, 0, 123456, UTC) - datetime(1899, 12, 30, tzinfo=UTC)
    ).total_seconds() / 86400
    assert float(cells["H2"].findtext("m:v", namespaces=_NS)) == pytest.approx(
        expected_serial, rel=0, abs=1e-9
    )

    cells = _sheet_cells(sheets[1])
    assert _inline_string(cells["E1"]) == "sent_at__excel_utc"
    for row, content in enumerate(messages, 2):
        assert _inline_string(cells[f"A{row}"]) == "-9223372036854775808"
        assert _inline_string(cells[f"C{row}"]) == sent_at
        assert float(cells[f"E{row}"].findtext("m:v", namespaces=_NS)) == pytest.approx(
            expected_serial, rel=0, abs=1e-9
        )
        if content is None:
            assert f"B{row}" not in cells
            assert _inline_string(cells[f"D{row}"]) == '["content"]'
        else:
            assert _inline_string(cells[f"B{row}"]) == content
            assert _inline_string(cells[f"D{row}"]) == "[]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("=1+1" + "x" * 32768, id="character-limit"),
        pytest.param("🟢" * 16384 + "_x000D_", id="utf16-limit"),
        pytest.param("https://example.invalid/" + "\r\n" * 254, id="line-feed-limit"),
    ],
)
async def test_xlsx_preserves_oversized_cells_in_ordered_literal_chunks(tmp_path, value):
    path = tmp_path / "snapshot.xlsx"
    row_id = "-9223372036854775808"
    columns = (("id", "int8"), ("content", "text"), ("processed_message_content", "text"))
    tables = [
        ExportTable("conversations", columns, _rows((row_id, value, "short"))),
        ExportTable("messages", columns, _rows((row_id, value, " " + value))),
    ]
    assert await write_workbook(path, tables, _CHECKED_AT) == {"conversations": 1, "messages": 1}

    with ZipFile(path) as archive:
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        assert [sheet.get("name") for sheet in book.findall(".//m:sheet", _NS)] == [
            "conversations",
            "messages",
            "__long_cells",
        ]
        assert b"ascending part order" in archive.read("docProps/core.xml")
        assert not any(name.startswith("xl/worksheets/_rels/") for name in archive.namelist())
        for sheet_number in (1, 2):
            sheet = ElementTree.fromstring(archive.read(f"xl/worksheets/sheet{sheet_number}.xml"))
            cells = _sheet_cells(sheet)
            assert _inline_string(cells["A2"]) == row_id
            assert _inline_string(cells["B2"]) == "[Full value: __long_cells]"
            assert _inline_string(cells["D2"]) == "[]"
        overflow = ElementTree.fromstring(archive.read("xl/worksheets/sheet3.xml"))

    assert overflow.find(".//m:f", _NS) is None
    assert overflow.find(".//m:hyperlink", _NS) is None
    assert [_inline_string(_sheet_cells(overflow)[f"{col}1"]) for col in "ABCDE"] == [
        "table",
        "row_id",
        "column",
        "part",
        "text",
    ]
    chunks = {}
    for row in overflow.findall(".//m:row", _NS)[1:]:
        cells = _sheet_cells(row)
        pos = row.attrib["r"]
        key = tuple(_inline_string(cells[col + pos]) for col in "ABC")
        part = int(cells["D" + pos].findtext("m:v", namespaces=_NS))
        chunk = _inline_string(cells["E" + pos])
        assert len(chunk.encode("utf-16-le")) // 2 <= 32767
        assert chunk.count("\n") <= 253
        parts = chunks.setdefault(key, [])
        assert part == len(parts) + 1
        parts.append(chunk)
    assert {key: "".join(parts) for key, parts in chunks.items()} == {
        ("conversations", row_id, "content"): value,
        ("messages", row_id, "content"): value,
        ("messages", row_id, "processed_message_content"): " " + value,
    }


@pytest.mark.asyncio
async def test_xlsx_keeps_previous_snapshot_if_continuation_sheet_overflows(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_export_xlsx, "_MAX_ROWS", 2)
    path = tmp_path / "snapshot.xlsx"
    path.write_bytes(b"previous snapshot")
    table = ExportTable(
        "messages", (("id", "int8"), ("content", "text")), _rows(("1", "x" * 32768))
    )

    with pytest.raises(ValueError, match="__long_cells: Excel row limit"):
        await write_workbook(path, [table], _CHECKED_AT)

    assert path.read_bytes() == b"previous snapshot"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_xlsx_counts_header_toward_worksheet_row_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_export_xlsx, "_MAX_ROWS", 2)
    path = tmp_path / "snapshot.xlsx"
    table = ExportTable("messages", (("content", "text"),), _rows(("one",), ("two",)))

    with pytest.raises(ValueError, match="Excel row limit"):
        await write_workbook(path, [table], _CHECKED_AT)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_xlsx_preserves_empty_table_headers_and_null_timestamps(tmp_path):
    path = tmp_path / "snapshot.xlsx"
    conversations = ExportTable("conversations", (("id", "int8"),), _rows())
    messages = ExportTable("messages", (("created_at", "timestamp"),), _rows((None,)))

    assert await write_workbook(path, [conversations, messages], _CHECKED_AT) == {
        "conversations": 0,
        "messages": 1,
    }
    with ZipFile(path) as archive:
        empty = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        populated = ElementTree.fromstring(archive.read("xl/worksheets/sheet2.xml"))

    assert list(_sheet_cells(empty)) == ["A1", "B1"]
    populated_cells = _sheet_cells(populated)
    assert list(populated_cells) == ["A1", "B1", "C1", "B2"]
    assert _inline_string(populated_cells["B2"]) == '["created_at"]'

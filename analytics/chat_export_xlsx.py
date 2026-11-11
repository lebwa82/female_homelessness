"""Stream a literal, lossless Chatwoot table snapshot to a private XLSX file."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from xlsxwriter import Workbook
from xlsxwriter.format import Format
from xlsxwriter.worksheet import Worksheet

_MAX_ROWS = 1_048_576
_MAX_COLUMNS = 16_384
_MAX_CELL_CHARS = 32_767
_MAX_LINE_FEEDS = 253
_MAX_EXACT_EXCEL_INT = 999_999_999_999_999
_INT_TYPES = {"int2", "int4", "int8"}
_TIMESTAMP_TYPES = {"timestamp", "timestamptz"}
_WRAPPED_COLUMNS = {"content", "processed_message_content"}
_LONG_CELLS_SHEET = "__long_cells"
_LONG_CELLS_HEADERS = ("table", "row_id", "column", "part", "text")


class _CellTooLong(ValueError):
    pass


@dataclass(frozen=True)
class ExportTable:
    name: str
    columns: tuple[tuple[str, str], ...]  # (column_name, PostgreSQL udt_name)
    rows: AsyncIterable[Sequence[str | None]]


def _write_string(sheet: Worksheet, row: int, col: int, value: str, fmt: Format) -> None:
    # Match Excel's limit in UTF-16 units, including non-BMP characters.
    if len(value.encode("utf-16-le")) // 2 > _MAX_CELL_CHARS or value.count("\n") > _MAX_LINE_FEEDS:
        raise _CellTooLong(
            f"{sheet.name}: Excel cell text limit at row {row + 1}, column {col + 1}"
        )
    if value.startswith("<r>") and value.endswith("</r>"):
        # Constant-memory XlsxWriter interprets this shape as raw rich-text XML
        # even with write_string(). Generate two properly escaped runs instead.
        result = sheet.write_rich_string(row, col, fmt, value[0], fmt, value[1:], fmt)
    else:
        result = sheet.write_string(row, col, value, fmt)
    if result:
        raise ValueError(f"{sheet.name}: could not write text at row {row + 1}, column {col + 1}")


def _text_chunks(value: str) -> Iterator[str]:
    start = units = line_feeds = 0
    for index, char in enumerate(value):
        width = 2 if ord(char) > 0xFFFF else 1
        newline = char == "\n"
        if units + width > _MAX_CELL_CHARS or (newline and line_feeds == _MAX_LINE_FEEDS):
            yield value[start:index]
            start, units, line_feeds = index, 0, 0
        units += width
        line_feeds += newline
    yield value[start:]


def _append_long_cell(
    sheet: Worksheet,
    row: int,
    key: tuple[str, str, str],
    value: str,
    literal: Format,
    plain: Format,
) -> int:
    for part, chunk in enumerate(_text_chunks(value), 1):
        row += 1
        if row >= _MAX_ROWS:
            raise ValueError(f"{_LONG_CELLS_SHEET}: Excel row limit")
        for col, entry in enumerate(key):
            _write_string(sheet, row, col, entry, literal)
        sheet.write_number(row, 3, part, plain)
        _write_string(sheet, row, 4, chunk, literal)
    return row


def _write_value(sheet: Worksheet, row: int, col: int, value: str, kind: str, fmt: Format) -> None:
    if kind == "bool":
        if value not in {"t", "f", "true", "false"}:
            raise ValueError(f"{sheet.name}: invalid boolean at row {row + 1}, column {col + 1}")
        sheet.write_boolean(row, col, value in {"t", "true"}, fmt)
        return
    if kind == "int":
        try:
            number = int(value)
        except ValueError:
            raise ValueError(
                f"{sheet.name}: invalid integer at row {row + 1}, column {col + 1}"
            ) from None
        if abs(number) <= _MAX_EXACT_EXCEL_INT:
            sheet.write_number(row, col, number, fmt)
            return
        # Keep large non-ID integers exact too; Excel would round them.
    _write_string(sheet, row, col, value, fmt)


def _excel_datetime(value: str, has_timezone: bool) -> datetime:
    parsed = datetime.fromisoformat(value)
    if (parsed.tzinfo is not None) != has_timezone:
        raise ValueError("Unexpected PostgreSQL timestamp timezone")
    if has_timezone:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    if parsed.year < 1900:
        raise ValueError("Timestamp predates Excel's 1900 date system")
    return parsed


async def write_workbook(
    path: Path, tables: Sequence[ExportTable], checked_at: datetime
) -> dict[str, int]:
    """Write a snapshot in row order. Leave an existing target intact on failure."""
    if checked_at.utcoffset() is None:
        raise ValueError("Snapshot timestamp must be timezone-aware")
    snapshot_utc = checked_at.astimezone(UTC)
    counts: dict[str, int] = {}
    # Put both XlsxWriter's XML spools and the incomplete XLSX in a mode-0700
    # directory on the destination filesystem. The published XLSX is mode 0600.
    with TemporaryDirectory(prefix=".chat-export-", dir=path.parent) as tmpdir:
        staged = Path(tmpdir) / "snapshot.xlsx"
        staged.touch(mode=0o600, exist_ok=False)
        with Workbook(
            str(staged),
            {
                "tmpdir": tmpdir,
                "constant_memory": True,
                "strings_to_formulas": False,
                "strings_to_urls": False,
            },
        ) as workbook:
            workbook.set_properties(
                {
                    "title": "Chatwoot: conversations and messages",
                    "created": snapshot_utc.replace(tzinfo=None),
                    "comments": (
                        "Source: PostgreSQL public schema. "
                        "__null_fields lists SQL NULL fields. "
                        "__excel_utc columns are sortable UTC timestamps; "
                        "timestamp values without a zone are treated as UTC. "
                        "The original timestamp text is retained. "
                        "For cells marked 'Full value: __long_cells', find the rows in "
                        "__long_cells matching table, row_id (the source id) and column. "
                        "Join the text in ascending part order without adding separators. "
                        "Attachment files are not included."
                    ),
                }
            )
            workbook.set_custom_property("snapshot_utc", snapshot_utc.isoformat())
            base = {"font_name": "Arial", "font_size": 10, "font_color": "#222222"}
            plain = workbook.add_format({**base, "valign": "vcenter"})
            literal = workbook.add_format({**base, "valign": "vcenter", "num_format": "@"})
            wrapped = workbook.add_format(
                {**base, "valign": "top", "num_format": "@", "text_wrap": True}
            )
            date = workbook.add_format(
                {**base, "valign": "vcenter", "num_format": "yyyy-mm-dd hh:mm:ss.000"}
            )
            header = workbook.add_format(
                {
                    **base,
                    "font_color": "#FFFFFF",
                    "bg_color": "#29475F",
                    "bold": True,
                    "text_wrap": True,
                    "align": "center",
                    "valign": "vcenter",
                }
            )

            sheets = [workbook.add_worksheet(table.name) for table in tables]
            long_cells = None
            long_cell_rows = 0
            for table, sheet in zip(tables, sheets, strict=True):
                names = [name for name, _ in table.columns]
                timestamp_columns = [
                    (index, udt == "timestamptz")
                    for index, (_, udt) in enumerate(table.columns)
                    if udt in _TIMESTAMP_TYPES
                ]
                headers = [
                    *names,
                    "__null_fields",
                    *(f"{names[index]}__excel_utc" for index, _ in timestamp_columns),
                ]
                if len(headers) > _MAX_COLUMNS:
                    raise ValueError(f"{table.name}: Excel column limit")
                if len(set(headers)) != len(headers):
                    raise ValueError(f"{table.name}: conflicting Excel column names")

                sheet.hide_gridlines(2)
                sheet.freeze_panes(1, 1)
                sheet.set_default_row(25)
                sheet.set_row(0, 48)
                for index, name in enumerate(headers):
                    _write_string(sheet, 0, index, name, header)

                columns: list[tuple[str, Format]] = []
                for index, (name, udt) in enumerate(table.columns):
                    kind, fmt, width = "text", literal, 21
                    if name in _WRAPPED_COLUMNS:
                        fmt, width = wrapped, 72
                    elif udt in _TIMESTAMP_TYPES:
                        width = 29
                    elif udt in {"json", "jsonb"}:
                        width = 45
                    elif udt == "bool":
                        kind, fmt = "bool", plain
                    elif udt in _INT_TYPES and name != "id" and not name.endswith("_id"):
                        kind, fmt = "int", plain
                    sheet.set_column(index, index, width)
                    columns.append((kind, fmt))
                sheet.set_column(len(names), len(names), 40)
                if timestamp_columns:
                    sheet.set_column(len(names) + 1, len(headers) - 1, 30)

                count = 0
                async for values in table.rows:
                    count += 1
                    if count >= _MAX_ROWS:
                        raise ValueError(f"{table.name}: Excel row limit")
                    if len(values) != len(names):
                        raise ValueError(
                            f"{table.name}: unexpected source row width at row {count}"
                        )
                    nulls = []
                    for index, (value, (kind, fmt)) in enumerate(zip(values, columns, strict=True)):
                        if value is None:
                            nulls.append(names[index])
                        else:
                            try:
                                _write_value(sheet, count, index, value, kind, fmt)
                            except _CellTooLong:
                                if names[0] != "id" or not (row_id := values[0]):
                                    raise ValueError(
                                        f"{table.name}: long cell has no source row id"
                                    ) from None
                                if long_cells is None:
                                    long_cells = workbook.add_worksheet(_LONG_CELLS_SHEET)
                                    long_cells.freeze_panes(1, 0)
                                    for col, name in enumerate(_LONG_CELLS_HEADERS):
                                        _write_string(long_cells, 0, col, name, header)
                                        long_cells.set_column(
                                            col, col, 72 if name == "text" else 21
                                        )
                                long_cell_rows = _append_long_cell(
                                    long_cells,
                                    long_cell_rows,
                                    (table.name, row_id, names[index]),
                                    value,
                                    literal,
                                    plain,
                                )
                                _write_string(
                                    sheet, count, index, f"[Full value: {_LONG_CELLS_SHEET}]", fmt
                                )
                    _write_string(
                        sheet,
                        count,
                        len(names),
                        json.dumps(nulls, ensure_ascii=False, separators=(",", ":")),
                        literal,
                    )
                    for col, (index, has_tz) in enumerate(timestamp_columns, len(names) + 1):
                        if (value := values[index]) is not None:
                            try:
                                date_value = _excel_datetime(value, has_tz)
                            except ValueError, OverflowError:
                                raise ValueError(
                                    f"{table.name}: invalid Excel timestamp at row {count + 1}, "
                                    f"column {index + 1}"
                                ) from None
                            sheet.write_datetime(count, col, date_value, date)
                sheet.autofilter(0, 0, count, len(headers) - 1)
                counts[table.name] = count
                workbook.set_custom_property(f"{table.name}_rows", count)

            if long_cells is not None:
                long_cells.autofilter(0, 0, long_cell_rows, len(_LONG_CELLS_HEADERS) - 1)

        staged.replace(path)
    return counts

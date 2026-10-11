"""The structure of a spreadsheet, for Phase 2 to summarise instead of its cells.

Phase 2 summarised a long workbook's cell dump in 40,000-character parts, up to 50 of them plus
a merge: about 45 calls for each of the 918 workbooks over a million characters, the largest
share of the attachment spend, for summaries that turned table rows into "decisions" (audit
2026-10-11). A long spreadsheet now goes in one call over this digest: per sheet its name and
size, its header row, up to SAMPLE_ROWS rows, each column's type with its basic statistics,
and up to FREE_TEXT_CHARS of its free text. The full dump stays in
attachment_content.extracted_text for keyword search.

The digest is read from that stored dump, never from the file: the sweep deletes a file once its
text is stored, usually before the nightly Phase 2 runs. The dump is the readers' format
(src/extract/attachment_extractors.py): a "--- Sheet: <name> ---" line per sheet, then a line per
non-empty row, its non-empty cells joined by " | ". An empty cell leaves no trace and a cell
holding a line break splits its row, so column statistics are taken over the rows that fill
every column, and the digest says how many those are.
"""

import math
import re
from collections import Counter

# The extraction methods of the workbook readers; xlrd also reads the .xlsb files that are really
# .xls ("xlrd (fallback from .xlsb)").
SPREADSHEET_METHODS = frozenset({"openpyxl", "pyxlsb", "xlrd"})

SAMPLE_ROWS = 20
# A long sheet shows its first rows and its last, where totals usually sit.
SAMPLE_HEAD = 15
FREE_TEXT_CHARS = 2_000
# A cell of this many words is prose (a note, a description, a remark), not a table value.
FREE_TEXT_MIN_WORDS = 6
# The header is looked for among a sheet's first rows: titles and notes often sit above it.
HEADER_SCAN_ROWS = 10
MAX_COLUMNS = 40
CELL_CHARS = 80
ROW_CHARS = 600
# A sheet section is at most about 20,000 characters, so the first one always fits. Sheets that
# no longer fit are named with their size and header only.
DIGEST_MAX_CHARS = 30_000

_SHEET = re.compile(r"^--- Sheet: (.*) ---$", re.M)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?")
# A column is of a type when at least this share of its values are.
_TYPE_SHARE = 0.8


def is_spreadsheet(method: str | None) -> bool:
    """Whether a row's text is a workbook reader's cell dump."""
    return method is not None and method.split(" ", 1)[0] in SPREADSHEET_METHODS


def spreadsheet_digest(text: str) -> str:
    """The digest of a workbook's stored cell dump. Pure: text in, text out.

    Sheets are described in full, in order, for as long as every sheet after them can still be
    named in a line; past DIGEST_MAX_CHARS the rest are named only, and past that counted."""
    sheets = _sheets(text or "")
    head = f"Spreadsheet digest: {len(sheets)} sheet{'' if len(sheets) == 1 else 's'}."
    full = [_sheet_section(name, rows) for name, rows in sheets]
    lines = [_sheet_line(name, rows) for name, rows in sheets]
    room = DIGEST_MAX_CHARS - len(head)
    named = sum(len(line) + 2 for line in lines)
    out, size = [head], 0
    for i, line in enumerate(lines):
        named -= len(line) + 2
        if size + len(full[i]) + 2 + named <= room:
            section = full[i]
        elif size + len(line) + 2 <= room - len(f"[{len(sheets)} more sheets not described]"):
            section = line
        else:
            out.append(f"[{len(sheets) - i} more sheets not described]")
            break
        out.append(section)
        size += len(section) + 2
    return "\n\n".join(out)


def _sheets(text: str) -> list[tuple[str, list[list[str]]]]:
    """(name, rows of cells) per sheet, in order. Text before the first marker is one sheet."""
    pieces = _SHEET.split(text)
    sheets = []
    if pieces[0].strip():
        sheets.append(("(unnamed)", _rows(pieces[0])))
    for name, body in zip(pieces[1::2], pieces[2::2], strict=True):
        sheets.append((name, _rows(body)))
    return sheets


def _rows(body: str) -> list[list[str]]:
    return [line.split(" | ") for line in body.split("\n") if line.strip()]


def _number(cell: str) -> float | None:
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _num(value: float) -> str:
    if value.is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.4g}"


def _day(cell: str) -> str:
    """A date as the dump writes it, its midnight time dropped."""
    return cell[:10] if cell[10:] in ("", " 00:00:00", "T00:00:00") else cell[:19]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _cell(cell: str) -> str:
    cell = cell.strip()
    return _clip(_day(cell) if _DATE.fullmatch(cell) else cell, CELL_CHARS)


def _line(cells: list[str]) -> str:
    return _clip(" | ".join(map(_cell, cells)), ROW_CHARS)


def _width(rows: list[list[str]]) -> int:
    """The most common number of cells in a row, the wider on a tie: the table's width."""
    counts = Counter(len(r) for r in rows)
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _header_index(rows: list[list[str]], width: int) -> int | None:
    """The first of the opening rows that is as wide as the table and mostly words."""
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        if len(row) == width and 2 * sum(_number(c) is None for c in row) >= len(row):
            return i
    return None


def _column(name: str, values: list[str]) -> str:
    if not values:
        return f"- {name}: empty"
    numbers = [n for n in map(_number, values) if n is not None]
    if len(numbers) >= _TYPE_SHARE * len(values):
        mean = sum(numbers) / len(numbers)
        return (
            f"- {name}: number, min {_num(min(numbers))}, max {_num(max(numbers))},"
            f" mean {_num(mean)}"
        )
    dates = [v for v in values if _DATE.fullmatch(v)]
    if len(dates) >= _TYPE_SHARE * len(values):
        return f"- {name}: date, {_day(min(dates))} to {_day(max(dates))}"
    return f"- {name}: text, {len(set(values)):,} distinct"


def _sheet_line(name: str, rows: list[list[str]]) -> str:
    """A sheet named with its size and header only, for a digest that is out of room."""
    if not rows:
        return f'Sheet "{name}": empty'
    width = _width(rows)
    at = _header_index(rows, width)
    header = _clip(_line(rows[at]), 200) if at is not None else "none found"
    return f'Sheet "{name}": {len(rows):,} rows, {max(map(len, rows))} columns; header: {header}'


def _sheet_section(name: str, rows: list[list[str]]) -> str:
    if not rows:
        return f'Sheet "{name}": empty'
    width = _width(rows)
    at = _header_index(rows, width)
    lines = [f'Sheet "{name}": {len(rows):,} rows, {max(map(len, rows))} columns']
    if at is None:
        lines.append("No header row found; columns are numbered.")
        labels = [f"column {j + 1}" for j in range(width)]
        preamble, data = [], rows
    else:
        lines.append(f"Header: {_line(rows[at])}")
        labels = [_clip(c.strip(), CELL_CHARS) for c in rows[at]]
        preamble, data = rows[:at], rows[at + 1 :]

    filled = [r for r in data if len(r) == width]
    if filled:
        lines.append(f"Columns, from the {len(filled):,} rows that fill every column:")
        for j, label in enumerate(labels[:MAX_COLUMNS]):
            lines.append(_column(label, [r[j].strip() for r in filled if r[j].strip()]))
        if width > MAX_COLUMNS:
            lines.append(f"[{width - MAX_COLUMNS} more columns]")
    elif data:
        lines.append("Columns: no row fills every column.")

    if len(data) <= SAMPLE_ROWS:
        shown = data
        lines.append(f"Rows, all {len(data):,}:" if data else "No rows below the header.")
        lines.extend(_line(r) for r in data)
    else:
        tail = SAMPLE_ROWS - SAMPLE_HEAD
        shown = data[:SAMPLE_HEAD] + data[-tail:]
        lines.append(f"Sample rows, {SAMPLE_ROWS} of {len(data):,}:")
        lines.extend(_line(r) for r in data[:SAMPLE_HEAD])
        lines.append(f"[{len(data) - SAMPLE_ROWS:,} rows not shown]")
        lines.extend(_line(r) for r in data[-tail:])

    free = _free_text(preamble, data, {c.strip() for r in shown for c in r})
    if free:
        lines.append("Free text:")
        lines.append(free)
    return "\n".join(lines)


def _free_text(preamble: list[list[str]], data: list[list[str]], shown: set[str]) -> str:
    """The rows above the table, then the prose cells the sample rows do not show, once each,
    to FREE_TEXT_CHARS."""
    pieces = [" | ".join(c.strip() for c in r) for r in preamble]
    seen = set(pieces) | shown
    for row in data:
        for cell in row:
            cell = cell.strip()
            if len(cell.split()) >= FREE_TEXT_MIN_WORDS and cell not in seen:
                seen.add(cell)
                pieces.append(cell)
        if sum(len(p) + 1 for p in pieces) > FREE_TEXT_CHARS:
            break
    return "\n".join(pieces)[:FREE_TEXT_CHARS].strip()

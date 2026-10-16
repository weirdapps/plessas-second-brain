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

A sheet too wide to show whole, more than MAX_COLUMNS columns or rows longer than ROW_CHARS,
keeps its label columns (up to MAX_LEAD leading columns of text) and as many of its newest
columns as fit a row, and says which it shows. The newest are its last columns, or its first
when the dates of its header run newest first. Until 2026-10-16 such a sheet kept its first 40
columns and the first 600 characters of each row: a daily report mailed in October, one column
a day since January, was summarised for January alone (audit 2026-10-16). The dates of a header
and of a date column are also named by month, in Greek and English, so a summary written from
the digest names the period ("Σεπτέμβριος 2026") that a search for it uses.
"""

import math
import re
from collections import Counter
from typing import NamedTuple

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
# A wide sheet keeps at most this many leading columns of text, its labels ("Channel", "Branch").
MAX_LEAD = 3
# What stands in a row for the columns a wide sheet leaves out.
GAP = "..."
# A sheet section is at most about 20,000 characters, so the first one always fits. Sheets that
# no longer fit are named with their size and header only.
DIGEST_MAX_CHARS = 30_000

_SHEET = re.compile(r"^--- Sheet: (.*) ---$", re.M)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?")
# A column is of a type when at least this share of its values are.
_TYPE_SHARE = 0.8

# Month names, nominative, as a search for a period writes them ("Σεπτέμβριος 2026").
_MONTHS_EL = (
    "Ιανουάριος Φεβρουάριος Μάρτιος Απρίλιος Μάιος Ιούνιος"
    " Ιούλιος Αύγουστος Σεπτέμβριος Οκτώβριος Νοέμβριος Δεκέμβριος"
).split()
_MONTHS_EN = (
    "January February March April May June July August September October November December"
).split()


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


def _period(first: str, last: str) -> str:
    """Two ISO dates as months, in Greek then English: "Ιανουάριος 2026 to Σεπτέμβριος 2026
    (January to September 2026)", or one month when both fall in it."""
    (y1, m1), (y2, m2) = (int(first[:4]), int(first[5:7]) - 1), (int(last[:4]), int(last[5:7]) - 1)
    if not (0 <= m1 < 12 and 0 <= m2 < 12):
        return ""
    if (y1, m1) == (y2, m2):
        return f"{_MONTHS_EL[m1]} {y1} ({_MONTHS_EN[m1]} {y1})"
    english = (
        f"{_MONTHS_EN[m1]} to {_MONTHS_EN[m2]} {y2}"
        if y1 == y2
        else f"{_MONTHS_EN[m1]} {y1} to {_MONTHS_EN[m2]} {y2}"
    )
    return f"{_MONTHS_EL[m1]} {y1} to {_MONTHS_EL[m2]} {y2} ({english})"


def _span(dates: list[str]) -> str:
    """The first and last of some ISO dates, and their months."""
    first, last = min(dates), max(dates)
    months = _period(first, last)
    return f"{first} to {last}, {months}" if months else f"{first} to {last}"


def _is_date(cell: str) -> bool:
    return bool(_DATE.fullmatch(cell.strip()))


class _Layout(NamedTuple):
    """The columns of a sheet the digest shows: every one (window None), or a wide sheet's
    `lead` label columns and `window` of its newest columns, its last (`tail`) or the first
    after its labels. A row narrower than the table keeps its own last cells in a tail layout:
    an empty cell leaves no trace, so a short row is taken to lack its oldest values."""

    width: int
    lead: int = 0
    window: int | None = None
    tail: bool = True

    def split(self, row: list[str]) -> tuple[list[str], list[str], bool]:
        """A row's label cells, its shown cells, and whether any of its cells are left out."""
        if self.window is None:
            return row, [], False
        rest = row[self.lead :]
        shown = rest[-self.window :] if self.tail else rest[: self.window]
        return row[: self.lead], shown, len(rest) > self.window

    def columns(self) -> list[int]:
        """The indices, in a row as wide as the table, of the columns shown."""
        if self.window is None:
            return list(range(self.width))
        start = self.width - self.window if self.tail else self.lead
        return [*range(self.lead), *range(start, start + self.window)]


def _render(row: list[str], layout: _Layout, limit: int = ROW_CHARS) -> str:
    """A row as the digest shows it, in `limit` characters. A wide row in a tail layout drops
    its oldest shown cells to fit, never its newest."""
    if layout.window is None:
        return _clip(" | ".join(map(_cell, row)), limit)
    labels, shown, cut = layout.split(row)
    lead = [*map(_cell, labels), *([GAP] if cut and layout.tail else [])]
    cells = list(map(_cell, shown))
    if not layout.tail and cut:
        cells.append(GAP)
    text = " | ".join([*lead, *cells])
    while layout.tail and len(text) > limit and len(cells) > 1:
        cells.pop(0)
        text = " | ".join([*lead, *cells])
    return _clip(text, limit)


def _lead(header: list[str] | None, rows: list[list[str]], width: int) -> int:
    """How many leading columns are labels: text in their header and in most of their values."""
    n = 0
    while n < min(MAX_LEAD, width - 1):
        if header is not None and _is_date(header[n]):
            break
        values = [r[n].strip() for r in rows if len(r) > n and r[n].strip()]
        figures = sum(_number(v) is not None or _is_date(v) for v in values)
        if not values or 2 * figures >= len(values):
            break
        n += 1
    return n


def _layout(header: list[str] | None, shown: list[list[str]], filled: list, width: int) -> _Layout:
    """Every column when the sheet fits, else its labels and the newest columns that fit a row
    in ROW_CHARS, MAX_COLUMNS in all at most."""
    lines = ([header] if header is not None else []) + shown
    whole = _Layout(width)
    if width <= MAX_COLUMNS and all(len(" | ".join(map(_cell, r))) <= ROW_CHARS for r in lines):
        return whole
    lead = _lead(header, filled or shown, width)
    dates = [_day(c.strip()) for c in (header or [])[lead:] if _is_date(c)]
    tail = not (len(dates) >= 2 and dates[0] > dates[-1])
    room = ROW_CHARS - len(GAP) - 3
    room -= max((len(" | ".join(map(_cell, r[:lead]))) + 3 for r in lines), default=0)
    window = 0
    while window < min(MAX_COLUMNS - lead, width - lead):
        at = -1 - window if tail else lead + window
        cells = [r[at] for r in lines if (len(r) - lead > window)]
        need = max((len(_cell(c)) + 3 for c in cells), default=0)
        if window and need > room:
            break
        room -= need
        window += 1
    if lead + window >= width:
        return whole
    return _Layout(width, lead, window, tail)


def _shown_columns(layout: _Layout, labels: list[str], dated: bool) -> str:
    """The line that says which columns a wide sheet shows and which it leaves out."""
    lead, window, width = layout.lead, layout.window or 0, layout.width
    start = width - window if layout.tail else lead
    out = range(lead, start) if layout.tail else range(start + window, width)
    kept = ["column 1 (labels)" if lead == 1 else f"columns 1 to {lead} (labels)"] if lead else []
    kept.append(f"{start + 1} to {start + window}")
    which = "the newest" if dated else ("the last" if layout.tail else "the first")
    text = (
        f"Columns shown: {' and '.join(kept)} of {width}, {which};"
        f" columns {out.start + 1} to {out.stop} are left out"
    )
    first, last = labels[out.start].strip(), labels[out.stop - 1].strip()
    if _is_date(first) and _is_date(last):
        text += f" ({_day(first)} to {_day(last)})"
    return text + "."


def _dates_line(header: list[str], layout: _Layout) -> str | None:
    """The span of the dates in a header, by month, and of the ones shown, or None for a
    header with fewer than two dates."""
    dates = [_day(c.strip()) for c in header if _is_date(c)]
    if len(dates) < 2:
        return None
    text = f"Dates in the header: {_span(dates)}"
    if layout.window is not None:
        shown = [_day(header[j].strip()) for j in layout.columns() if _is_date(header[j])]
        if shown and len(shown) < len(dates):
            text += f"; shown: {_span(shown)}"
    return text + "."


class _Table(NamedTuple):
    width: int
    header: list[str] | None
    labels: list[str]
    preamble: list[list[str]]
    data: list[list[str]]
    filled: list[list[str]]
    shown: list[list[str]]
    layout: _Layout


def _table(rows: list[list[str]]) -> _Table:
    """A non-empty sheet's rows read as a table: its header, data, sample and layout."""
    width = _width(rows)
    at = _header_index(rows, width)
    if at is None:
        header, labels = None, [f"column {j + 1}" for j in range(width)]
        preamble, data = [], rows
    else:
        header, labels = rows[at], [_cell(c) for c in rows[at]]
        preamble, data = rows[:at], rows[at + 1 :]
    filled = [r for r in data if len(r) == width]
    if len(data) <= SAMPLE_ROWS:
        shown = data
    else:
        shown = data[:SAMPLE_HEAD] + data[-(SAMPLE_ROWS - SAMPLE_HEAD) :]
    return _Table(
        width, header, labels, preamble, data, filled, shown, _layout(header, shown, filled, width)
    )


def cuts_columns(text: str) -> bool:
    """Whether the digest of this stored dump leaves out columns of any sheet: a sheet too wide
    to show whole. Before 2026-10-16 the digest of such a sheet left out its newest columns
    (src/extract/reextract.py, the `wide` selector)."""
    return any(rows and _table(rows).layout.window is not None for _, rows in _sheets(text or ""))


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
        return f"- {name}: date, {_span([_day(d) for d in dates])}"
    return f"- {name}: text, {len(set(values)):,} distinct"


def _sheet_line(name: str, rows: list[list[str]]) -> str:
    """A sheet named with its size and header only, for a digest that is out of room."""
    if not rows:
        return f'Sheet "{name}": empty'
    table = _table(rows)
    header = _render(table.header, table.layout, 200) if table.header is not None else "none found"
    return f'Sheet "{name}": {len(rows):,} rows, {max(map(len, rows))} columns; header: {header}'


def _sheet_section(name: str, rows: list[list[str]]) -> str:
    if not rows:
        return f'Sheet "{name}": empty'
    table = _table(rows)
    layout, data, filled, shown = table.layout, table.data, table.filled, table.shown
    lines = [f'Sheet "{name}": {len(rows):,} rows, {max(map(len, rows))} columns']
    if table.header is None:
        lines.append("No header row found; columns are numbered.")
    else:
        lines.append(f"Header: {_render(table.header, layout)}")
    dated = False
    if table.header is not None:
        dates = _dates_line(table.header, layout)
        if dates:
            lines.append(dates)
            dated = True
    if layout.window is not None:
        lines.append(_shown_columns(layout, table.labels, dated))

    if filled:
        lines.append(f"Columns, from the {len(filled):,} rows that fill every column:")
        for j in layout.columns():
            values = [r[j].strip() for r in filled if r[j].strip()]
            lines.append(_column(table.labels[j], values))
    elif data:
        lines.append("Columns: no row fills every column.")

    if len(data) <= SAMPLE_ROWS:
        lines.append(f"Rows, all {len(data):,}:" if data else "No rows below the header.")
        lines.extend(_render(r, layout) for r in data)
    else:
        lines.append(f"Sample rows, {SAMPLE_ROWS} of {len(data):,}:")
        lines.extend(_render(r, layout) for r in shown[:SAMPLE_HEAD])
        lines.append(f"[{len(data) - SAMPLE_ROWS:,} rows not shown]")
        lines.extend(_render(r, layout) for r in shown[SAMPLE_HEAD:])

    free = _free_text(table.preamble, data, {c.strip() for r in shown for c in r})
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

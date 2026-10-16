"""Figures from a recurring report's stored cell dump: what each number is, for when, and where.

Numbers were 12 of the owner's 93 questions in six weeks, the costliest at a median of 12
brain calls each, and four of his twelve complaints. They sit in recurring workbooks (a
monthly migration report, a daily payments workbook, contact-centre and budget workbooks),
stored only as a cell dump and an LLM summary, so agents dug them out with LIKE scans and
the newest edition often ranked below older ones (meta review, 2026-10-16). This module
reads the dump into rows of (sheet, row label, column header, period, value, unit, cell),
deterministically: no model call, the same text in giving the same rows out.

The dump is the readers' format (src/extract/attachment_extractors.py): a "--- Sheet: <name>
---" line per sheet, then a line per non-empty row, its non-empty cells joined by " | ". An
empty cell leaves no trace and a cell holding a line break splits its row. So:

- A data row is placed under its header only when that is unambiguous: as wide as the
  header; or missing only leading labels that the row above shows (a group named once), its
  values then under the header's last columns; or as wide as its header's value columns when
  the row labels have no header cell of their own. Any other row is left out and counted
  (`unaligned_rows`), never guessed: a figure under the wrong header is worse than none.
- A header split over several lines by line breaks is joined again: the lines just above
  the data, merged at their boundary cells, until they are as wide as the data.
- The cell a figure names is its place in the stored text, R<row>C<cell>, counting the
  non-empty rows and cells as stored, with the character offset get_item pages from. The
  workbook's own A1 address cannot be recovered from the dump.

The period comes, in order, from a date or month in the column header ("header"), in the
row label ("row"), in the sheet's name or a date standing alone above its table ("sheet"),
in the filename ("filename"), and last the email's date ("email"); `period_source` says
which. A month, quarter, or day and month without a year takes the year of its row label, of
a year row above it, of a filename that names a year alone, or else the latest year that
keeps it on or before the filename's date or the email's. A run of three or more dates a
month apart names months ("2026-08"), as monthly reports date them by a first or last day.

A table with more than TABLE_MAX_ROWS rows that are not periods is a record list (customers,
cases, an extract) and is left out, as is a sheet of more than SHEET_MAX_ROWS rows; a time
series keeps its newest MAX_PERIODS periods, and an edition at most MAX_FIGURES figures.
"""

import calendar
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator
from datetime import date
from typing import NamedTuple

from src.store.greek import search_fold

# Bumped when the parse changes what it reads, so a backfill can tell old rows from new.
PARSER_VERSION = 1

# A sheet of more non-empty rows is a data extract, not a report: left unread, cheaply.
SHEET_MAX_ROWS = 3_000
# A table of more rows whose labels are not periods is a record list (customers, cases).
TABLE_MAX_ROWS = 400
# A time series down the rows or across the columns keeps its newest periods.
MAX_PERIODS = 400
MAX_FIGURES_PER_SHEET = 6_000
MAX_FIGURES = 20_000
# A header split by line breaks spans at most this many stored lines.
HEADER_LINES = 12
# How far above a table its header is looked for.
HEADER_SCAN_ROWS = 12
LABEL_CHARS = 160
HEADER_CHARS = 120
# A table whose rows lead with more label cells lists records (orders, cases), not figures.
MAX_LABEL_COLUMNS = 3
# Header words for a figure to the report's date, not for the year beside them.
_TO_DATE = frozenset({"mtd", "qtd", "ytd", "wtd"})
# A one-cell row this short inside a table is a section label; above it, a title.
SECTION_CHARS = 80
TITLE_CHARS = 160

SEPARATOR = " | "
_SHEET = re.compile(r"^--- Sheet: (.*) ---$", re.M)
_EN_DASH, _EM_DASH = chr(0x2013), chr(0x2014)

# ------------------------------------------------------------------ periods

# Month names as search_fold() writes them: lower case, no accents, final sigma as σ.
_MONTH_WORDS: dict[str, int] = {}
for _month_number, _forms in enumerate(
    (
        "jan january ιαν ιανουαριοσ ιανουαριου",
        "feb february φεβ φεβρ φεβρουαριοσ φεβρουαριου",
        "mar march μαρ μαρτιοσ μαρτιου",
        "apr april απρ απριλιοσ απριλιου",
        "may μαι μαιοσ μαιου",
        "jun june ιουν ιουνιοσ ιουνιου",
        "jul july ιουλ ιουλιοσ ιουλιου",
        "aug august αυγ αυγουστοσ αυγουστου",
        "sep sept september σεπ σεπτ σεπτεμβριοσ σεπτεμβριου",
        "oct october οκτ οκτωβριοσ οκτωβριου",
        "nov november νοε νοεμ νοεμβριοσ νοεμβριου",
        "dec december δεκ δεκεμβριοσ δεκεμβριου",
    ),
    start=1,
):
    for _word in _forms.split():
        _MONTH_WORDS[_word] = _month_number
_MONTH = "|".join(sorted(map(re.escape, _MONTH_WORDS), key=len, reverse=True))

_L = r"(?<![a-zα-ω0-9])"  # nothing word-like before
_R = r"(?![a-zα-ω0-9])"  # nothing word-like after
_ND = r"(?![0-9])"  # no digit after
_ISO = re.compile(_L + r"(\d{4})-(\d{2})-(\d{2})(?:[ t]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?" + _ND)
_YMD_SPACED = re.compile(_L + r"(\d{4})[ ._](\d{2})[ ._](\d{2})" + _ND)
_DMY = re.compile(_L + r"(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})" + _ND)
_DAY_MONTH_YEAR = re.compile(
    _L + r"(\d{1,2})[ \-.]?(" + _MONTH + r")\.?[ \-.,']?(\d{4}|\d{2})" + _ND
)
_MONTH_YEAR = re.compile(_L + r"(" + _MONTH + r")\.?[ \-.,/']{0,2}(\d{4}|\d{2})" + _ND)
_YEAR_MONTH_NAME = re.compile(_L + r"(\d{4})[ \-.,/]{1,2}(" + _MONTH + r")" + _R)
_YM = re.compile(_L + r"(\d{4})[-/.](\d{1,2})(?![0-9./-])")
_MY = re.compile(_L + r"(\d{1,2})[-/.](\d{4})" + _ND)
_QUARTER = re.compile(
    _L
    + r"(?:q([1-4])[ \-/']{0,2}(\d{4}|\d{2})|(\d{4})[ \-/]{0,2}q([1-4])"
    + r"|([1-4])q[ \-/']?(\d{4}|\d{2})|([1-4])ο\s*τριμηνο\s*(\d{4}))"
    + _ND
)
_HALF = re.compile(
    _L
    + r"(?:h([12])[ \-/']{0,2}(\d{4}|\d{2})|([12])h[ \-/']?(\d{4}|\d{2})|(\d{4})[ \-]h([12]))"
    + _ND
)
_DM = re.compile(r"^(\d{1,2})[./](\d{1,2})\.?$")
_MONTH_ALONE = re.compile(_L + r"(" + _MONTH + r")\.?" + _R)
_QUARTER_ALONE = re.compile(_L + r"(?:q([1-4])|([1-4])q)" + _R)
_YEAR = re.compile(_L + r"((?:19|20)\d{2})(?:\.0)?" + _ND)
# In a filename only: a year and a month apart ("Report 2026 08").
_YM_SPACED = re.compile(_L + r"(\d{4})[ _](\d{1,2})" + _ND)
# Compact dates in filenames: yyyymmddhhmm, yyyymmdd or ddmmyyyy, yyyymm.
_DIGITS = re.compile(r"(?<![0-9])(\d{12}|\d{8}|\d{6})(?![0-9])")


class Period(NamedTuple):
    """A period: its label ('2026-08-31', '2026-08', '2026-Q3', '2026-H1', '2026') and the
    ISO date it ends on, which orders periods of any length."""

    label: str
    end: str


class Found(NamedTuple):
    """A period read from a text, how long it is, and the words left beside it (folded)."""

    period: Period
    kind: str  # day, month, quarter, half, year
    rest: str


class _When(NamedTuple):
    kind: str
    year: int | None
    month: int = 0
    day: int = 0
    part: int = 0


def _year(text: str) -> int:
    value = int(text)
    return value + 2000 if value < 100 else value


def _valid(year: int | None, month: int = 1, day: int = 1) -> bool:
    year = 2000 if year is None else year  # a leap year: 29/02 without a year is a day
    if not (1990 <= year <= 2100 and 1 <= month <= 12):
        return False
    return 1 <= day <= calendar.monthrange(year, month)[1]


def _period(when: _When) -> Period | None:
    year = when.year
    if year is None:
        return None
    if when.kind == "day":
        if not _valid(year, when.month, when.day):
            return None
        label = f"{year:04d}-{when.month:02d}-{when.day:02d}"
        return Period(label, label)
    if when.kind == "month":
        last = calendar.monthrange(year, when.month)[1]
        return Period(f"{year:04d}-{when.month:02d}", f"{year:04d}-{when.month:02d}-{last:02d}")
    if when.kind == "quarter":
        month = when.part * 3
        last = calendar.monthrange(year, month)[1]
        return Period(f"{year:04d}-Q{when.part}", f"{year:04d}-{month:02d}-{last:02d}")
    if when.kind == "half":
        end = "06-30" if when.part == 1 else "12-31"
        return Period(f"{year:04d}-H{when.part}", f"{year:04d}-{end}")
    return Period(f"{year:04d}", f"{year:04d}-12-31")


def _matches(text: str) -> Iterator[tuple[_When, tuple[int, int]]]:
    """Every period with a year named in a folded text, the most precise kinds first."""
    for m in _ISO.finditer(text):
        if _valid(int(m[1]), int(m[2]), int(m[3])):
            yield _When("day", int(m[1]), int(m[2]), int(m[3])), m.span()
    for m in _YMD_SPACED.finditer(text):
        if _valid(int(m[1]), int(m[2]), int(m[3])):
            yield _When("day", int(m[1]), int(m[2]), int(m[3])), m.span()
    for m in _DMY.finditer(text):
        day, month, year = int(m[1]), int(m[2]), _year(m[3])
        if _valid(year, month, day):
            yield _When("day", year, month, day), m.span()
    for m in _DAY_MONTH_YEAR.finditer(text):
        day, month, year = int(m[1]), _MONTH_WORDS[m[2]], _year(m[3])
        if _valid(year, month, day):
            yield _When("day", year, month, day), m.span()
    for m in _MONTH_YEAR.finditer(text):
        month, year = _MONTH_WORDS[m[1]], _year(m[2])
        if _valid(year, month):
            yield _When("month", year, month), m.span()
    for m in _YEAR_MONTH_NAME.finditer(text):
        if _valid(int(m[1]), _MONTH_WORDS[m[2]]):
            yield _When("month", int(m[1]), _MONTH_WORDS[m[2]]), m.span()
    for m in _YM.finditer(text):
        if _valid(int(m[1]), int(m[2])):
            yield _When("month", int(m[1]), int(m[2])), m.span()
    for m in _MY.finditer(text):
        if _valid(int(m[2]), int(m[1])):
            yield _When("month", int(m[2]), int(m[1])), m.span()
    for m in _QUARTER.finditer(text):
        g = m.groups()
        if g[0]:
            q, y = g[0], g[1]
        elif g[2]:
            q, y = g[3], g[2]
        elif g[4]:
            q, y = g[4], g[5]
        else:
            q, y = g[6], g[7]
        if _valid(_year(y)):
            yield _When("quarter", _year(y), part=int(q)), m.span()
    for m in _HALF.finditer(text):
        g = m.groups()
        if g[0]:
            h, y = g[0], g[1]
        elif g[2]:
            h, y = g[2], g[3]
        else:
            h, y = g[5], g[4]
        if _valid(_year(y)):
            yield _When("half", _year(y), part=int(h)), m.span()


def _yearless(text: str) -> tuple[_When, tuple[int, int]] | None:
    """A day and month, a month or a quarter named without a year, in a folded text."""
    m = _DM.match(text.strip())
    if m:
        day, month = int(m[1]), int(m[2])
        if _valid(None, month, day):
            return _When("day", None, month, day), (0, len(text))
    m = _MONTH_ALONE.search(text)
    if m:
        return _When("month", None, _MONTH_WORDS[m[1]]), m.span()
    m = _QUARTER_ALONE.search(text)
    if m:
        return _When("quarter", None, part=int(m[1] or m[2])), m.span()
    return None


_PUNCTUATION = re.compile(r"[\s_:;,|/\\\-" + _EN_DASH + _EM_DASH + r".()\[\]{}*#'\"]+")


def _clean(text: str) -> str:
    """Words with the punctuation around them gone, single-spaced."""
    return " ".join(_PUNCTUATION.sub(" ", text).split())


def _start(kind: str, period: Period) -> date:
    end = date.fromisoformat(period.end)
    if kind == "month":
        return end.replace(day=1)
    if kind == "quarter":
        return date(end.year, end.month - 2, 1)
    if kind == "half":
        return date(end.year, end.month - 5, 1)
    if kind == "year":
        return date(end.year, 1, 1)
    return end


def _with_year(when: _When, year: int | None, before: date | None) -> _When:
    """`when` given a year: `year` when one is known, else the latest that keeps it on or
    before `before`; without either it keeps none."""
    if when.year is not None:
        return when
    if year is not None:
        return when._replace(year=year)
    if before is None:
        return when
    for candidate_year in (before.year, before.year - 1):
        candidate = when._replace(year=candidate_year)
        period = _period(candidate)
        if period is not None and _start(when.kind, period) <= before:
            return candidate
    return when._replace(year=before.year - 1)


def parse_period(text: str, *, year: int | None = None, before: date | None = None) -> Found | None:
    """The period a header, a label or a name gives, with the words left beside it.

    A date, a month and year, a quarter, a half, then a year alone; a month, quarter, or day
    and month without a year takes `year`, else the latest year that keeps it on or before
    `before`. None when the text names no period.
    """
    folded = search_fold(unicodedata.normalize("NFC", text or "")).strip()
    if not folded or len(folded) > HEADER_CHARS:
        return None
    best = next(_matches(folded), None)
    if best is None:
        bare = _yearless(folded)
        if bare is not None:
            when = _with_year(bare[0], year, before)
            if when.year is not None:
                best = (when, bare[1])
    if best is None:
        m = _YEAR.search(folded)
        if m:
            best = (_When("year", int(m[1])), m.span())
    if best is None:
        return None
    when, (start, end) = best
    period = _period(when)
    if period is None:
        return None
    return Found(period, when.kind, _clean(folded[:start] + " " + folded[end:]))


def _filename_matches(folded: str) -> list[tuple[_When, tuple[int, int]]]:
    """The periods a folded filename names, compact digit runs included; a year or a digit
    run inside a date already found is not named again."""
    found: list[tuple[_When, tuple[int, int]]] = []
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for when, span in _matches(folded):  # the most precise first: "08.2026" in "31.08.2026"
        if free(span):
            found.append((when, span))
            taken.append(span)
    for m in _YM_SPACED.finditer(folded):  # "Report 2026 08"
        if free(m.span()) and _valid(int(m[1]), int(m[2])):
            found.append((_When("month", int(m[1]), int(m[2])), m.span()))
            taken.append(m.span())
    for m in _DIGITS.finditer(folded):
        if not free(m.span()):
            continue
        digits = m[1]
        compact: _When | None = None
        if len(digits) in (8, 12):
            if _valid(int(digits[:4]), int(digits[4:6]), int(digits[6:8])):
                compact = _When("day", int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            elif len(digits) == 8 and _valid(int(digits[4:]), int(digits[2:4]), int(digits[:2])):
                compact = _When("day", int(digits[4:]), int(digits[2:4]), int(digits[:2]))
        elif _valid(int(digits[:4]), int(digits[4:])):
            compact = _When("month", int(digits[:4]), int(digits[4:]))
        if compact is not None:
            found.append((compact, m.span()))
            taken.append(m.span())
    for m in _YEAR.finditer(folded):
        if free(m.span()):
            found.append((_When("year", int(m[1])), m.span()))
            taken.append(m.span())
    return found


def _name(filename: str) -> str:
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", unicodedata.normalize("NFC", filename or ""))


def filename_period(filename: str) -> Period | None:
    """The period a filename names: the latest it names (a range names its end), or None."""
    periods = [
        p for when, _span in _filename_matches(search_fold(_name(filename))) if (p := _period(when))
    ]
    if not periods:
        return None
    return max(periods, key=lambda p: (p.end, -len(p.label)))


# ------------------------------------------------------------------ series

_VERSION_WORDS = (
    "final",
    "draft",
    "revised",
    "updated",
    "update",
    "new",
    "old",
    "copy",
    "τελικο",
    "τελικη",
    "τελικα",
    "αντιγραφο",
    "ytd",
    "mtd",
)
_VERSION = re.compile(
    r"^copy of |"
    + _L
    + r"(?:v\s?\d+(?:\.\d+)*[a-z]?|"
    + "|".join(_VERSION_WORDS)
    + r")"
    + _R
    + r"|\(\s*\d+\s*\)"
)
_HHMM = re.compile(r"(?<![0-9])\d{4}(?![0-9])")
_PERIOD_WORDS = re.compile(
    _L + r"(?:" + _MONTH + r"|q[1-4]|[1-4]q|h[12]|[1-4]ο\s*τριμηνο|[12]ο\s*εξαμηνο)" + _R
)
_DANGLING_DAY = re.compile(r"(?<![0-9])\d{1,2}\s*[-.](?=\s|$)")


def _blank(pattern: re.Pattern, text: str) -> str:
    """`text` with every match of `pattern` turned to as many spaces: lengths are kept, so a
    span found here is the same span of the text it was folded from."""
    return pattern.sub(lambda m: " " * len(m[0]), text)


def _strip_periods(folded: str) -> str:
    """A folded name with every date, period, time, version and copy token blanked out."""
    found = _filename_matches(folded)
    chars = list(folded)
    for _when, (start, end) in found:
        chars[start:end] = " " * (end - start)
    text = _blank(_PERIOD_WORDS, "".join(chars))
    if found:  # a time of day left after its date: "STAR 20260911 1600"
        text = _blank(_HHMM, text)
    text = _blank(_VERSION, text)
    return _blank(_DANGLING_DAY, text)  # "13-" of "13-16.02.2026"


def sheet_key(name: str) -> str:
    """A sheet's name as one metric's editions share it: folded, without dates or periods,
    so a sheet named for its day ("14.10.2026") is one sheet in every edition."""
    return _clean(_strip_periods(search_fold(unicodedata.normalize("NFC", name or ""))))


def sheet_title(name: str) -> str:
    """A sheet's name as shown for a metric of every edition: as it is, unless it holds a date
    or period, which differs from edition to edition; then without it, or "(sheet named by
    its date)" when nothing else is left."""
    nfc = unicodedata.normalize("NFC", name or "")
    if sheet_key(nfc) == _clean(search_fold(nfc)):
        return nfc
    return series_title(nfc + ".x") if sheet_key(nfc) else "(sheet named by its date)"


def period_bounds(label: str) -> tuple[str, str] | None:
    """The first and last day (ISO) of a period label as Period writes them, or None."""
    m = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?|-([QH])([1-4]))?", label or "")
    if not m:
        return None
    year = int(m[1])
    if m[3]:
        return label, label
    if m[2]:
        month = int(m[2])
        if not 1 <= month <= 12:
            return None
        last = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"
    if m[4]:
        span = 3 if m[4] == "Q" else 6
        part = int(m[5])
        if m[4] == "H" and part > 2:
            return None
        first_month, last_month = (part - 1) * span + 1, part * span
        last = calendar.monthrange(year, last_month)[1]
        return f"{year:04d}-{first_month:02d}-01", f"{year:04d}-{last_month:02d}-{last:02d}"
    return f"{year:04d}-01-01", f"{year:04d}-12-31"


def series_stem(filename: str) -> str:
    """The name the editions of one report share: the filename folded (case, accents, final
    sigma), without its extension, dates, periods, times, version suffixes and copy markers."""
    return _clean(_strip_periods(search_fold(_name(filename))))


def series_title(filename: str) -> str:
    """The series' name as a person reads it: the filename in its own case and accents,
    without the tokens series_stem drops; the stem when the two do not line up."""
    name = _name(filename)
    folded = search_fold(name)
    if len(folded) != len(name):
        return series_stem(filename) or name
    blanked = _strip_periods(folded)
    kept = "".join(
        " " if b == " " and not c.isspace() else c for c, b in zip(name, blanked, strict=True)
    )
    words = [w for w in kept.replace("_", " ").split() if re.search(r"\w", w)]
    return " ".join(words) or series_stem(filename) or name


# ------------------------------------------------------------------ the dump


class Figure(NamedTuple):
    """One numeric cell: what it measures, for which period, and where it is stored."""

    sheet: str
    label: str
    header: str
    label_key: str
    header_key: str
    period: str
    period_end: str
    period_source: str
    value: float
    unit: str | None
    cell: str
    offset: int


class Parsed(NamedTuple):
    figures: list[Figure]
    sheets: int
    tables: int
    skipped: dict[str, int]


SKIP_REASONS = (
    "unaligned_rows",
    "record_lists",
    "duplicates",
    "capped",
    "long_sheets",
    "no_header",
)

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_YEAR_CELL = re.compile(r"(?:19|20)\d{2}(?:\.0)?")
_PLACEHOLDERS = frozenset(
    {"n/a", "na", "nan", "null", "none", "x", "-", "--", _EN_DASH, _EM_DASH, "...", "*"}
)
# Excel's error values as the readers write them: "#DIV/0!" and the like, and pyxlsb's codes.
_ERROR_CELL = re.compile(r"#[a-z0-9/_]+[!?]?|0x[0-9a-f]{1,2}", re.IGNORECASE)


def _placeholder(cell: str) -> bool:
    """A cell that stands for a value it does not give: a dash, n/a, an Excel error."""
    text = cell.strip()
    return text.lower() in _PLACEHOLDERS or _ERROR_CELL.fullmatch(text) is not None


_LETTER = re.compile(r"[^\W\d_]")
_PERCENT = ("%", "ποσοστ", "percent")
_EURO = ("€", " eur ", "ευρω")


def _number(cell: str) -> float | None:
    text = cell.strip()
    if not _NUMBER.fullmatch(text):
        return None
    value = float(text)
    return value if abs(value) != float("inf") else None


def _is_year(cell: str) -> bool:
    return _YEAR_CELL.fullmatch(cell.strip()) is not None


def _year_run(cells: list[str]) -> bool:
    """Two or more years one apart each, as a header of years has them (2024 | 2025)."""
    if len(cells) < 2 or not all(_is_year(c) for c in cells):
        return False
    years = [int(float(c)) for c in cells]
    steps = {b - a for a, b in zip(years, years[1:], strict=False)}
    return steps in ({1}, {-1})


def _is_label(cell: str) -> bool:
    """A cell that names its row: words, a date, or a day and month."""
    text = cell.strip()
    if not text or _placeholder(text):
        return False
    if _LETTER.search(text) or _DM.match(text):
        return True
    folded = search_fold(text)
    return bool(_ISO.match(folded) or _DMY.match(folded))


class _Row:
    """A stored row: its place in the sheet and in the text, its cells, how many of them
    lead as labels, and whether it holds a figure."""

    __slots__ = ("cells", "data", "labels", "mixed", "number", "offset")

    def __init__(self, number: int, offset: int, cells: list[str]):
        self.number = number
        self.offset = offset
        self.cells = cells
        # A year leading a row with figures after it names the row ("2025 | 10 | 11").
        n = 1 if len(cells) > 1 and _is_year(cells[0]) and not _year_run(cells) else 0
        while n < len(cells) and _number(cells[n]) is None and _is_label(cells[n]):
            n += 1
        self.labels = n
        numbers = [c for c in cells[n:] if _number(c) is not None]
        self.data = bool(numbers) and not _year_run(numbers)
        # Words after its figures: a code read as a figure split the labels ("A | 46 | Main
        # street | 25000"), or a note sits among the values. Which cell is which is unknown.
        self.mixed = self.data and any(_LETTER.search(c) and not _placeholder(c) for c in cells[n:])

    def cell_offset(self, j: int) -> int:
        return self.offset + sum(len(c) for c in self.cells[:j]) + len(SEPARATOR) * j


def _rows(text: str, start: int, end: int) -> list[_Row]:
    rows: list[_Row] = []
    at = start
    for line in text[start:end].split("\n"):
        if line.strip():
            rows.append(_Row(len(rows) + 1, at, line.split(SEPARATOR)))
        at += len(line) + 1
    return rows


def _sheets(text: str) -> Iterator[tuple[str, int, int]]:
    """(name, start, end) of each sheet's rows in the text."""
    marks = list(_SHEET.finditer(text))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        yield mark[1], min(mark.end() + 1, end), end


def _merge(lines: list[_Row]) -> list[str]:
    """Stored lines that are one row split by line breaks, joined at their boundary cells."""
    cells: list[str] = []
    for row in lines:
        if cells:
            cells[-1] = f"{cells[-1].rstrip()} {row.cells[0].lstrip()}"
            cells.extend(row.cells[1:])
        else:
            cells = list(row.cells)
    return [" ".join(c.split()) for c in cells]


def _headerish(cells: list[str]) -> bool:
    """Words or periods over a table: two cells or more, at most a third of them figures
    (a run of years aside) or placeholders."""
    if len(cells) < 2:
        return False
    numbers = [c for c in cells if _number(c) is not None]
    figures = 0 if _year_run(numbers) else len(numbers)
    placeholders = sum(_placeholder(c) for c in cells)
    return 3 * (figures + placeholders) <= len(cells)


class _Header(NamedTuple):
    cells: list[str]
    start: int  # the rows above the data it was read from: above[start:end]
    end: int
    corner: bool  # all its cells are over value columns: the row labels have no header


def _find_header(above: list[_Row], width: int, values: int) -> _Header | None:
    """The header of a table whose rows are `width` cells, `values` of them figures: the
    nearest row above as wide; else such a row split by line breaks, joined again; else a
    row as wide as the values, over a corner the labels leave without a header."""
    base = max(0, len(above) - HEADER_SCAN_ROWS)
    scan = range(len(above) - 1, base - 1, -1)
    for i in scan:
        cells = [" ".join(c.split()) for c in above[i].cells]
        if len(cells) == width and _headerish(cells):
            return _Header(cells, i, i + 1, False)
    for end in range(len(above), base, -1):
        for k in range(2, min(HEADER_LINES, end - base) + 1):
            lines = above[end - k : end]
            if any(r.data for r in lines):
                break
            merged = _merge(lines)
            if len(merged) == width and _headerish(merged):
                return _Header(merged, end - k, end, False)
            if len(merged) > width:
                break
    if values < width:
        for i in scan:
            cells = [" ".join(c.split()) for c in above[i].cells]
            if len(cells) == values and _headerish(cells):
                if _clean(search_fold(cells[0])) in _LABEL_HEADERS:
                    # "Month | Total | ..." one cell short: a header over the labels that lost
                    # a value column's header, and which one cannot be told.
                    return None
                return _Header(cells, i, i + 1, True)
    return None


# What the header over a column of periods says: such a header is never over a value.
_LABEL_HEADERS = frozenset(
    _clean(search_fold(word))
    for word in (
        "month",
        "months",
        "date",
        "dates",
        "day",
        "period",
        "year",
        "week",
        "μήνας",
        "μήνες",
        "ημερομηνία",
        "ημέρα",
        "περίοδος",
        "έτος",
        "εβδομάδα",
    )
)


def _groups(above: list[_Row], header: "_Header", first: int) -> list[str]:
    """The group each value column of the header sits under, from up to two rows of group
    names right above it ("Mutual funds | New generation fund" over "Transactions | Amount |
    Transactions | Amount"): a row is taken when its names split the value columns into equal
    spans whose headers repeat, which a merged cell over each span leaves; else no groups."""
    cells = header.cells[first:]
    width = len(cells)
    groups = [""] * width
    plain = [_clean(search_fold(c)) for c in cells]
    for row in reversed(above[max(0, header.start - 2) : header.start]):
        names = [" ".join(c.split()) for c in row.cells]
        k = len(names)
        if (
            k < 2
            or k >= width
            or width % k
            or row.data
            or not all(_LETTER.search(n) for n in names)
        ):
            break
        span = width // k
        if span < 2 or any(plain[i] != plain[i + span] for i in range(width - span)):
            break
        groups = [
            f"{names[i // span]} / {g}" if g else names[i // span] for i, g in enumerate(groups)
        ]
    return groups


def _keys(cells: list[str]) -> list[str]:
    """Each header's key, a repeated one numbered by its place: total, total #2."""
    seen: Counter[str] = Counter()
    keys = []
    for cell in cells:
        key = _clean(search_fold(cell))
        seen[key] += 1
        keys.append(key if seen[key] == 1 else f"{key} #{seen[key]}")
    return keys


def _unit(*texts: str) -> str | None:
    folded = f" {search_fold(' '.join(texts))} "
    if any(mark in folded for mark in _PERCENT):
        return "%"
    if any(mark in folded for mark in _EURO):
        return "€"
    return None


def _monthly(found: list[Found]) -> bool:
    """Whether three or more days fall in as many months, one to three months apart each:
    dates that name months (a report may date March the 31st and April the 1st)."""
    labels = [f.period.label for f in found if f.kind == "day"]
    months = sorted({int(label[:4]) * 12 + int(label[5:7]) for label in labels})
    if len(months) < 3 or len(months) != len(labels):
        return False
    return all(1 <= b - a <= 3 for a, b in zip(months, months[1:], strict=False))


def _as_month(found: Found) -> Found:
    if found.kind != "day":
        return found
    day = date.fromisoformat(found.period.label)
    last = calendar.monthrange(day.year, day.month)[1]
    label = f"{day.year:04d}-{day.month:02d}"
    return Found(Period(label, f"{label}-{last:02d}"), "month", found.rest)


def _tables(rows: list[_Row]) -> Iterator[tuple[list[_Row], list[_Row]]]:
    """A sheet's rows cut into (rows above, data with the one-cell rows among it). A one-cell
    row (a year, a section) does not end a table; any other row that holds no figure does,
    and starts the rows above the next, after the one-cell rows that closed this one."""
    above: list[_Row] = []
    run: list[_Row] = []

    def split() -> tuple[list[_Row], list[_Row]]:
        end = len(run)
        while end and not run[end - 1].data:
            end -= 1
        return run[:end], run[end:]

    for row in rows:
        if row.data or (len(row.cells) == 1 and run):
            run.append(row)
            continue
        if run:
            data, tail = split()
            yield above, data
            above, run = tail, []
        above.append(row)
    if run:
        yield above, split()[0]


class _Placed(NamedTuple):
    row: _Row
    labels: list[str]
    shift: int  # the header index of row cell j is j + shift
    year: int | None
    section: str | None
    found: Found | None  # the period the row label names


class _SheetReader:
    """Reads one sheet's tables in order, keeping what each says to the ones after it: its
    title, a date standing above the tables, its header."""

    def __init__(self, name: str, edition: date | None, email_day: str | None, file: Period | None):
        self.name = name
        self.email_day = email_day
        self.file = file
        # The latest a year left out can put a period: the filename's date, else the email's.
        self.before = date.fromisoformat(file.end) if file else edition
        if self.before and edition and self.before > edition:
            self.before = edition
        self.file_year = int(file.label) if file and len(file.label) == 4 else None
        sheet = self._period(name)
        self.standing: Found | None = sheet
        self.title: str | None = None
        self.header: _Header | None = None
        self.figures: list[Figure] = []
        self.seen: set[tuple[str, str, str]] = set()
        self.skipped: Counter[str] = Counter()

    def _period(self, cell: str, year: int | None = None) -> Found | None:
        """The period a cell names when that is what the cell is: a date, a month, a quarter
        or a year, with at most three words beside it (YTD, Actual)."""
        text = cell.strip()
        if not text or len(text) > 40 or (_NUMBER.fullmatch(text) and not _is_year(text)):
            return None
        found = parse_period(text, year=year or self.file_year, before=self.before)
        if found is None or len(found.rest.split()) > 3:
            return None
        return found

    def _note_above(self, rows: list[_Row]) -> None:
        """A title or a standing date among the rows over a header."""
        for row in rows:
            if len(row.cells) != 1:
                continue
            cell = " ".join(row.cells[0].split())
            found = self._period(cell)
            if found is not None and found.kind in ("day", "month") and not found.rest:
                self.standing = found
            elif _LETTER.search(cell) and len(cell) <= TITLE_CHARS:
                self.title = cell

    def read(self, above: list[_Row], data: list[_Row]) -> None:
        rows = [r for r in data if r.data and not r.mixed]
        if not rows:
            self.skipped["unaligned_rows"] += sum(r.data for r in data)
            return
        # The commonest row width, the wider on a tie, then the next: a short row is one
        # whose empty cells left no trace.
        widths = Counter(len(r.cells) for r in rows)
        header = None
        sizes = set()
        for width, _count in sorted(widths.items(), key=lambda wc: (-wc[1], -wc[0]))[:3]:
            values = Counter(len(r.cells) - r.labels for r in rows if len(r.cells) == width)
            sizes |= {width, values.most_common(1)[0][0]}
            header = _find_header(above, width, values.most_common(1)[0][0])
            if header is not None:
                break
        if header is None:
            self._note_above(above)
            # A table that goes on below a row of notes keeps the header above it.
            if self.header is None or len(self.header.cells) not in sizes:
                self.skipped["no_header"] += 1
                return
            header, inner = self.header, [r for r in above if len(r.cells) == 1]
        else:
            self._note_above(above[: header.start])
            inner = above[header.end :]
        self.header = header
        self._read_table(header, inner + data, above)

    def _place(self, header: _Header, rows: list[_Row]) -> tuple[list[_Placed], int]:
        """Each data row with its labels and its place under the header, and how many of the
        header's cells are over labels; rows that cannot be placed are counted, not kept."""
        cells = header.cells
        data = [r for r in rows if r.data and not r.mixed]
        full = [r for r in data if len(r.cells) == len(cells)]
        if header.corner:
            wide = 0
        elif full:
            wide = Counter(r.labels for r in full).most_common(1)[0][0]
        else:
            given = Counter(len(r.cells) - r.labels for r in data).most_common(1)[0][0]
            wide = max(0, len(cells) - given)
        value_columns = len(cells) - wide
        single = len(data) == 1
        placed: list[_Placed] = []
        year: int | None = None
        section: str | None = None
        previous: list[str] = []
        for row in rows:
            if not row.data:
                cell = " ".join(row.cells[0].split())
                if _is_year(cell):
                    year = int(float(cell))
                elif (
                    _LETTER.search(cell)
                    and len(cell) <= SECTION_CHARS
                    and self._period(cell) is None
                ):
                    section = cell
                continue
            if row.mixed:
                self.skipped["unaligned_rows"] += 1
                continue
            n = row.labels
            labels = [" ".join(c.split()) for c in row.cells[:n]]
            given = len(row.cells) - n
            if not header.corner and len(row.cells) == len(cells) and n == wide and n:
                shift = 0
            elif given == value_columns and 0 < n < wide and len(previous) >= wide:
                labels = previous[: wide - n] + labels  # a group named once, above
                shift = len(cells) - len(row.cells)
            elif header.corner and n and given == value_columns:
                shift = -n
            elif (
                n == 0
                and given == len(cells) - 1
                and _is_label(cells[0])
                and self._period(cells[0]) is None
            ):
                labels, shift = [cells[0]], 1  # the header's first cell names its one row
            elif n == 0 and given == value_columns and single and self.title:
                labels, shift = [], wide
            else:
                self.skipped["unaligned_rows"] += 1
                continue
            if n == wide:
                previous = labels
            found = self._period(labels[0], year) if n else None
            placed.append(_Placed(row, labels, shift, year, section, found))
        return placed, wide

    def _read_table(self, header: _Header, rows: list[_Row], above: list[_Row]) -> None:
        placed, wide = self._place(header, rows)
        if not placed:
            return
        dated = [p.found for p in placed if p.found is not None]
        by_period = 2 * len(dated) >= len(placed)
        if wide > MAX_LABEL_COLUMNS or (not by_period and len(placed) > TABLE_MAX_ROWS):
            self.skipped["record_lists"] += 1
            return
        if by_period and len(placed) > MAX_PERIODS:
            self.skipped["capped"] += len(placed) - MAX_PERIODS
            placed = placed[-MAX_PERIODS:]
        if _monthly(dated):
            placed = [p._replace(found=_as_month(p.found)) if p.found else p for p in placed]
        cells = header.cells
        first = 0 if header.corner else wide
        heads = [self._period(c) if i >= first else None for i, c in enumerate(cells)]
        across = [h for h in heads if h is not None]
        if _monthly(across):
            heads = [_as_month(h) if h is not None else None for h in heads]
        # "MtD 2026" names no year's figure but one to the report's date.
        sources = ["header"] * len(cells)
        as_of = self._as_of()
        for i, h in enumerate(heads):
            if h is not None and h.kind == "year" and _TO_DATE & set(h.rest.split()) and as_of:
                heads[i] = Found(as_of[0].period, as_of[0].kind, h.rest)
                sources[i] = as_of[1]
        if len(across) > MAX_PERIODS:
            first = max(first, len(cells) - MAX_PERIODS)
        groups = [""] * first + _groups(above, header, first)
        shown = _qualified(cells, groups, first)
        keys = _keys(shown)
        for p in placed:
            self._emit(p, cells, shown, keys, groups, heads, sources, first)

    def _as_of(self) -> tuple[Found, str] | None:
        """The date the report is as of, and where it was read: a date standing on the
        sheet, else the filename's period, else the email's day."""
        if self.standing is not None:
            return self.standing, "sheet"
        if self.file is not None:
            return Found(self.file, "", ""), "filename"
        if self.email_day:
            return Found(Period(self.email_day, self.email_day), "day", ""), "email"
        return None

    def _emit(
        self,
        p: _Placed,
        cells: list[str],
        shown: list[str],
        keys: list[str],
        groups: list[str],
        heads: list[Found | None],
        sources: list[str],
        first: int,
    ) -> None:
        row_year = (
            int(p.found.period.label) if p.found is not None and p.found.kind == "year" else None
        )
        names = p.labels[1:] if p.found is not None else list(p.labels)
        if p.section:
            names = [p.section, *names]
        label = " / ".join(names)[:LABEL_CHARS]
        if not p.labels and self.title:
            label = self.title[:LABEL_CHARS]
        label_key = _clean(search_fold(label))
        for j in range(p.row.labels, len(p.row.cells)):
            h = j + p.shift
            if h < first or h >= len(cells):
                continue
            value = _number(p.row.cells[j])
            if value is None:
                continue
            head = heads[h]
            if head is not None and (row_year or p.year) and _without_year(cells[h]):
                # A month or quarter with no year of its own: the row's, or a year row's.
                head = self._period(cells[h], row_year or p.year)
            key_label, header_key, header = label_key, keys[h], shown[h]
            if head is not None:
                found, source = head, sources[h]
                # The header without its period, so every period of one column is one metric.
                header = " / ".join(x for x in (groups[h], _rest_shown(cells[h])) if x)
                header_key = (
                    _clean(search_fold(header)) + keys[h][len(_clean(search_fold(shown[h]))) :]
                )
            elif p.found is not None:
                found, source = p.found, "row"
                key_label = _clean(f"{p.found.rest} {label_key}")
            elif (as_of := self._as_of()) is not None:
                found, source = as_of
            else:
                continue
            key = (key_label, header_key, found.period.label)
            if key in self.seen:
                self.skipped["duplicates"] += 1
                continue
            self.seen.add(key)
            self.figures.append(
                Figure(
                    sheet=self.name,
                    label=label,
                    header=header[:HEADER_CHARS],
                    label_key=key_label,
                    header_key=header_key,
                    period=found.period.label,
                    period_end=found.period.end,
                    period_source=source,
                    value=value,
                    unit=_unit(cells[h], groups[h], label, p.section or ""),
                    cell=f"R{p.row.number}C{j + 1}",
                    offset=p.row.cell_offset(j),
                )
            )


def _without_year(cell: str) -> bool:
    """Whether a header names a month, quarter or day and month but no year."""
    folded = search_fold(cell)
    return _yearless(folded) is not None and next(_matches(folded), None) is None


def _rest_shown(cell: str) -> str:
    """A header cell as written, without the period it names: "YTD Aug 2026" is "YTD"."""
    text = " ".join(cell.split())
    folded = search_fold(text)
    span = _period_span(folded)
    if span is None:
        return text
    if len(folded) != len(text):  # no character-for-character fold: the folded words
        found = parse_period(text)
        return found.rest if found else ""
    return _clean(text[: span[0]] + " " + text[span[1] :])


def _period_span(folded: str) -> tuple[int, int] | None:
    """Where in a folded text parse_period finds its period."""
    best = next(_matches(folded), None) or _yearless(folded)
    if best is None:
        m = _YEAR.search(folded)
        return m.span() if m else None
    return best[1]


def _qualified(cells: list[str], groups: list[str], first: int) -> list[str]:
    """The header as shown: each value column under its group, and a header repeated among
    the value columns named with the nearest distinct header before it, as a reader tells
    "Index (Corporate total)" from "Index (Retail total)"."""
    shown = [f"{g} / {c}" if g else c for c, g in zip(cells, groups, strict=True)]
    plain = [_clean(search_fold(c)) for c in shown]
    counts = Counter(plain[first:])
    out = list(shown)
    for i in range(first, len(shown)):
        if counts[plain[i]] < 2:
            continue
        before = next(
            (shown[b] for b in range(i - 1, first - 1, -1) if counts[plain[b]] == 1), None
        )
        if before:
            out[i] = f"{shown[i]} ({before})"
    return out


def _too_long(text: str, start: int, end: int) -> bool:
    if text.count("\n", start, end) <= SHEET_MAX_ROWS:
        return False
    return sum(1 for line in text[start:end].split("\n") if line.strip()) > SHEET_MAX_ROWS


def parse_workbook(text: str, *, filename: str, email_date: str | None) -> Parsed:
    """Every figure a stored workbook dump holds, with counts of what was left out and why."""
    skipped: Counter[str] = Counter(dict.fromkeys(SKIP_REASONS, 0))
    figures: list[Figure] = []
    try:
        edition = date.fromisoformat((email_date or "")[:10])
    except ValueError:
        edition = None
    email_day = edition.isoformat() if edition else None
    file = filename_period(filename)
    sheets = tables = 0
    text = text or ""
    for name, start, end in _sheets(text):
        sheets += 1
        if len(figures) >= MAX_FIGURES:
            skipped["capped"] += 1
            continue
        if _too_long(text, start, end):
            skipped["long_sheets"] += 1
            continue
        reader = _SheetReader(name, edition, email_day, file)
        for above, data in _tables(_rows(text, start, end)):
            reader.read(above, data)
            tables += 1
        room = min(MAX_FIGURES_PER_SHEET, MAX_FIGURES - len(figures))
        if len(reader.figures) > room:
            skipped["capped"] += len(reader.figures) - room
        figures.extend(reader.figures[:room])
        skipped.update(reader.skipped)
    return Parsed(figures, sheets, tables, dict(skipped))

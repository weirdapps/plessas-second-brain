"""Recency fused into keyword ranking, and the editions of one report shown as one row.

Keyword search ranked by text relevance alone (BM25), with the date as a tie-break.
A report mailed every month matches its own name as well in every edition, and the
older ones had the head start: on one store the newest edition of a monthly KPI
workbook came 18th and 19th for its own name, behind five older editions and the
press clippings, so recall's five attachment rows never showed it. So a search now
ranks its matches twice, by relevance as before and newest first, and fuses the two
lists by reciprocal rank fusion (src/store/fusion.py), the recency list at
RECENCY_WEIGHT. Recency reorders the matches it is given, the best `pool` by
relevance, and nothing else: it never brings in a row the words do not find.

A search whose matches come in stages, keyword search_emails' subject, then
summary, then body (src/store/query.py), keeps them: recency reorders the rows
inside each stage, so an old email whose subject holds the words still comes before
a newer one that only mentions them in the body.

Editions. Items whose name differs only by a date or a period are one series: an
attachment's filename, an email's subject with its Re:/Fwd: prefixes off
(series_key). The series is ranked once, by its best-ranked member and its newest
date, and shown by its newest member, which says `older_editions: n` and lists the
others' refs in `editions`, newest first. A query that names a date (31.05.2026)
shows the member whose name holds it instead. The page is cut after the collapse,
so it holds `limit` series rather than five editions of one.

BRAIN_QUERY_RANKING=relevance turns both off for one deployment: relevance alone, no
series, and in recall only the email bucket fused with semantic search, as before.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Hashable, Sequence
from typing import cast

from src.store.fusion import reciprocal_rank_fusion
from src.store.greek import search_fold

# The recency list's weight in the fusion, against 1 for relevance. Equal: the third
# list the 2026-10-16 review recommended, untuned until the qa set can tune it.
RECENCY_WEIGHT = 1.0
# How many of a search's best matches by relevance are ranked again, per row asked.
POOL_PER_ROW = 4
# And at least this many, so a five-row bucket still sees the editions behind the
# first twenty matches.
POOL_MIN = 40
# And at most this many, or the limit itself when that is more: recall's left-out
# probes ask for 50 rows on every call, and a pool of 200 doubled their time.
POOL_MAX = 100


def fused_ranking() -> bool:
    """Whether searches fuse recency and collapse editions: unless BRAIN_QUERY_RANKING
    is `relevance`. Read at each call, so a test, an eval or a process can change it."""
    return os.environ.get("BRAIN_QUERY_RANKING", "").strip().lower() != "relevance"


def pool_size(limit: int) -> int:
    """How many matches by relevance a search of `limit` rows ranks again."""
    return max(min(max(limit * POOL_PER_ROW, POOL_MIN), POOL_MAX), limit)


# --- dates and periods in a name ------------------------------------------------------------

_MONTHS = (
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec "
    "ιανουαριοσ ιανουαριου φεβρουαριοσ φεβρουαριου μαρτιοσ μαρτιου απριλιοσ απριλιου "
    "μαιοσ μαιου ιουνιοσ ιουνιου ιουλιοσ ιουλιου αυγουστοσ αυγουστου σεπτεμβριοσ "
    "σεπτεμβριου οκτωβριοσ οκτωβριου νοεμβριοσ νοεμβριου δεκεμβριοσ δεκεμβριου "
    "ιαν φεβ μαρ απρ μαι ιουν ιουλ αυγ σεπ σεπτ οκτ νοε νοεμ δεκ"
).split()
_WEEKDAYS = (
    "monday tuesday wednesday thursday friday saturday sunday "
    "mon tue tues wed thu thur thurs fri sat sun "
    "δευτερα τριτη τεταρτη πεμπτη παρασκευη σαββατο κυριακη"
).split()
_SEP = r"[-./_]"
_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_MONTH = r"(?:0?[1-9]|1[0-2])"
_YEAR = r"(?:19|20)\d{2}"
# Longest first: a match is taken whole, left to right, so 31.05.2026 is one date and
# not a day, a month and a year.
_NUMERIC = (
    rf"{_YEAR}{_SEP}{_MONTH}{_SEP}{_DAY}",  # 2026-08-31
    rf"{_DAY}{_SEP}{_MONTH}{_SEP}(?:{_YEAR}|\d{{2}})",  # 31.08.2026, 31/8/26
    rf"{_YEAR}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])?",  # 20260831, 202608
    rf"(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2]){_YEAR}",  # 31082026
    rf"{_MONTH}{_SEP}{_YEAR}",  # 08.2026
    rf"{_YEAR}{_SEP}{_MONTH}",  # 2026-08
    rf"{_DAY}[./]{_MONTH}",  # 31.08, a weekly deck's day and month
    _YEAR,  # 2026
    r"[1-4]q\d{0,2}|q[1-4]|[12]h\d{0,2}|h[12]|fy\d{2,4}|[1-9]m\d{2}|wk?\d{1,2}",  # Q3, 1H26
)
_WORDS = "|".join(sorted(map(re.escape, _MONTHS + _WEEKDAYS), key=len, reverse=True))
# A date stands between separators; an underscore is one (report_20260831.xlsx).
_PERIOD = re.compile(
    r"(?<![^\W_])(?:" + "|".join(f"(?:{p})" for p in _NUMERIC) + rf"|{_WORDS})(?![^\W_]|[.,/-]?\d)"
)
# A version or a copy of a file, in its name: v2, rev3, final, draft, (1), copy, τελικό.
# Filenames only: in a subject 'Draft agenda' and 'Final agenda' are two emails.
_VERSION = re.compile(
    r"(?<![^\W_])(?:v\d+(?:[._]\d+)*|ver\d*|version|rev\d*|final|draft|updated|revised|clean"
    r"|copy|τελικ[οηα]σ?|τελικα|προσχεδιο|αντιγραφο)(?![^\W_])|\(\d{1,2}\)"
)
_FINAL = re.compile(r"(?<![^\W_])(?:final|τελικ[οηα]σ?|τελικα)(?![^\W_])")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,5}$")
_NUMBER = re.compile(r"(?<![^\W_])\d{1,4}(?![^\W_])")
_NOT_WORD = re.compile(r"[\W_]+")
_LETTERS = re.compile(r"[^\W\d_]{3,}")


def _folded(text: str) -> str:
    return search_fold(text).strip()


def series_parts(name: str | None, *, subject: bool = False) -> tuple[str | None, bool]:
    """(base, marked): a name without its dates, periods and versions, and whether it had any.

    The name is folded (case, accents, final sigma). Taken out: numeric dates in the
    common orders, a year, a month or weekday name in English or Greek, a quarter, half
    or financial year, a week number, and in a filename a version or copy mark (v2,
    final, draft, (1), τελικό). Once a name holds a date, every other number of up to
    four digits is taken for part of it too (the 9 of 'Tue 9 JUN'); a longer one is a
    reference and stays. A subject loses its Re: and Fwd: prefixes first, and a
    filename keeps its extension, so a workbook and the PDF made of it are two series.
    The base is None for a name with no word of three letters left (31.08.2026.pdf).
    """
    if not name:
        return None, False
    if subject:
        from src.store.schema import normalize_subject

        name = normalize_subject(name)
    text = _folded(name)
    extension = ""
    if not subject:
        found = _EXTENSION.search(text)
        if found:
            extension, text = found.group(0), text[: found.start()]
    text, dated = _PERIOD.subn(" ", text)
    versioned = 0
    if not subject:
        text, versioned = _VERSION.subn(" ", text)
    if dated:
        text = _NUMBER.sub(" ", text)
    words = " ".join(_NOT_WORD.sub(" ", text).split())
    if not _LETTERS.search(words):
        return None, False
    return words + extension, bool(dated or versioned)


def series_key(name: str | None, *, subject: bool = False) -> str | None:
    """The series a filename (or, with `subject`, an email subject) heads, or None.

    The base of series_parts, for a name it marked: one with a date, a period or a
    version. None for any other: image003.png repeats across unrelated mail, and two
    plain names are one series only as copies of one file or beside a marked name of
    the same base (editions_ranked).
    """
    base, marked = series_parts(name, subject=subject)
    return base if marked else None


def query_periods(query: str | None) -> list[str]:
    """The dates and periods a query names, folded, in order: a search for one edition."""
    return [m.group(0) for m in _PERIOD.finditer(_folded(query or ""))]


def is_final(name: str | None) -> bool:
    """Whether a filename says it is the final version."""
    return bool(name) and bool(_FINAL.search(_folded(name or "")))


def is_reply(subject: str | None) -> bool:
    """Whether a subject is a reply or a forward: it loses a prefix to normalize_subject."""
    from src.store.schema import normalize_subject

    text = (subject or "").strip().lower()  # normalize_subject lower-cases too
    return bool(text) and normalize_subject(text) != text


# --- the ranking ------------------------------------------------------------------------------


def _groups(
    rows: Sequence[dict],
    name: Callable[[dict], str | None] | None,
    copy: Callable[[dict], str | None] | None,
    subject: bool,
) -> list[list[int]]:
    """Row indexes by series, each in the order given, the series in order of their
    first row: rows sharing a copy hash, and rows sharing a base where one of them is
    marked (series_parts)."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    by_copy: dict[str, int] = {}
    by_base: dict[str, list[int]] = {}
    marked_bases: set[str] = set()
    for n, row in enumerate(rows):
        digest = copy(row) if copy else None
        if digest:
            join(by_copy.setdefault(digest, n), n)
        if name:
            base, marked = series_parts(name(row), subject=subject)
            if base:
                by_base.setdefault(base, []).append(n)
                if marked:
                    marked_bases.add(base)
    for base in marked_bases:
        first, *rest = by_base[base]
        for n in rest:
            join(first, n)
    groups: dict[int, list[int]] = {}
    for n in range(len(rows)):
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


def editions_ranked(
    rows: Sequence[dict],
    limit: int,
    *,
    date: Callable[[dict], str | None],
    name: Callable[[dict], str | None] | None = None,
    ref: Callable[[dict], Hashable] | None = None,
    stage: Callable[[dict], int] | None = None,
    copy: Callable[[dict], str | None] | None = None,
    query: str = "",
    subject: bool = False,
) -> list[dict]:
    """`rows`, best match first, ranked again with recency and their editions collapsed.

    Args:
        rows: a search's matches by relevance, best first: the pool to rank again.
        limit: how many rows (series) to return.
        date: a row's date, an ISO string or None; None ranks last for recency.
        name: the name a row's series is read from (series_parts), or None for none.
        ref: the id `editions` lists a row by.
        stage: the match stage a row came from; stages keep their order, and recency
            reorders rows inside each.
        copy: a row's content hash: rows sharing one are copies of one file.
        query: the query, for the dates it names (query_periods).
        subject: names are email subjects (their Re: and Fwd: prefixes go, and no
            version mark is read).

    A series is shown by its newest member; on the newest day, by one whose name says
    final; with a query naming dates, by the newest member whose name holds them all.
    Returns new dicts: the rows given are not changed. With BRAIN_QUERY_RANKING=relevance,
    the first `limit` rows as they came.
    """
    if not fused_ranking():
        return [dict(r) for r in rows[:limit]]
    ref = ref or id
    periods = query_periods(query)
    groups = _groups(rows, name, copy, subject)

    def when(n: int) -> str:
        return date(rows[n]) or ""

    def name_of(n: int) -> str:
        return (name(rows[n]) if name else None) or ""

    def shown(group: list[int]) -> int:
        named = [n for n in group if all(p in _folded(name_of(n)) for p in periods)]
        pick = named if periods and named else group
        # An edition's own mail before a reply: a reply to August's report sent after
        # September's is not the newest edition. Then the newest day, a final version,
        # the newest time, and relevance.
        return max(
            pick,
            key=lambda n: (
                not (subject and is_reply(name_of(n))),
                when(n)[:10],
                not subject and is_final(name_of(n)),
                when(n),
                -n,
            ),
        )

    def stage_of(group: list[int]) -> int:
        return min(stage(rows[n]) for n in group) if stage else 0

    order: list[int] = []
    numbered = range(len(groups))
    for level in sorted({stage_of(g) for g in groups}):
        inside = [g for g in numbered if stage_of(groups[g]) == level]
        by_date = sorted(inside, key=lambda g: max(when(n) for n in groups[g]), reverse=True)
        fused = reciprocal_rank_fusion([inside, by_date], weights=[1.0, RECENCY_WEIGHT])
        order.extend(cast(int, g) for g, _score in fused)
    out = []
    for g in order[:limit]:
        group = groups[g]
        pick = shown(group)
        result = dict(rows[pick])
        if len(group) > 1:
            others = sorted((n for n in group if n != pick), key=lambda n: (when(n), -n))
            others.reverse()
            result["older_editions"] = len(others)
            result["editions"] = [ref(rows[n]) for n in others]
        out.append(result)
    return out

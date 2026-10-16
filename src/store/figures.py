"""The numbers registry: figures from recurring report workbooks, the newest period first.

Numbers were 12 of the owner's 93 questions in six weeks, the costliest at a median of 12 brain
calls each, and four of his twelve complaints; answers ended in "not found" or "give me your best
estimate" while the figure sat in a workbook the store held as text (meta review, 2026-10-16).
This module keeps those figures as rows:

    figure_series    spreadsheet attachments grouped by filename stem and sender
                     (figures_parse.series_stem); a series is kept from MIN_EDITIONS editions
    figure_editions  one row per spreadsheet attachment: its series, its date, the earlier
                     edition whose bytes it copies, and whether its figures are stored
    figure_metrics   one row per (series, sheet, row label, column header), keyed folded
    figure_values    one row per metric and period: the value, the newest edition stating it,
                     its cell and its offset in the stored text
    figure_restated  the values a newer edition replaced

Supersession: for one metric a newer period supersedes an older one, and a newer edition that
restates a period with another value supersedes the older edition's, which moves to
figure_restated. An edition restating a period unchanged moves the row to itself, so the cell
cited is the newest edition's. Editions are ordered by their email's date, then id, whatever
order they are parsed in. Nothing here calls a model.
"""

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Iterable
from datetime import UTC, date, timedelta
from typing import Any

from src.extract.attachment_digest import SPREADSHEET_METHODS
from src.extract.figures_parse import (
    PARSER_VERSION,
    Figure,
    parse_period,
    parse_workbook,
    period_bounds,
    series_stem,
    series_title,
    sheet_key,
    sheet_title,
)
from src.store import envelope
from src.store.greek import register_sql_functions, search_fold, search_words
from src.store.stamps import instant, utc_now

# A series is kept from its third edition: two files of one name are a coincidence as often
# as a report.
MIN_EDITIONS = 3
# The hourly sync's share: the new editions of the series it touched, never the backlog.
INGEST_DEADLINE_S = 20.0
# How many metrics a search reads before ordering them newest first.
CANDIDATES = 200
MAX_LIMIT = 200

_SPREADSHEET = (
    "("
    + " OR ".join(
        f"ac.extraction_method = '{m}' OR ac.extraction_method GLOB '{m} *'"
        for m in sorted(SPREADSHEET_METHODS)
    )
    + ")"
)
_TABLES = ("figure_series", "figure_editions", "figure_metrics", "figure_values", "figure_restated")
NOT_BUILT = (
    "The figures registry is not built on this store yet: it comes with schema v36 and "
    "`brain maint figures-backfill --apply` on the producer, and reaches a replica with the next pull."
)


def has_registry(conn: sqlite3.Connection) -> bool:
    """Whether the store has the v36 tables (a replica may run newer code than its copy)."""
    found = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'figure_%'"
        )
    }
    return all(t in found for t in _TABLES) and "figure_metrics_fts" in found


def _stamp(*stamps: object) -> str | None:
    """The first stamp that reads as an instant, in UTC: "2026-10-12T09:30:00Z"."""
    for stamp in stamps:
        when = instant(stamp)
        if when is not None:
            return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


# ------------------------------------------------------------------ editions


def spreadsheet_editions(
    conn: sqlite3.Connection, attachment_ids: Iterable[int] | None = None, *, new_only: bool = True
) -> list[dict]:
    """The spreadsheet attachments whose text is stored, each with what groups it into a series:
    the stem of its filename, its sender, its date, its bytes' hash. Read from the covering
    indexes and the attachment rows only: no stored text."""
    sql = (
        "SELECT a.id, a.filename, a.sha256, e.sender_address, e.date_received, a.exported_at"
        " FROM attachment_content ac JOIN attachments a ON a.id = ac.attachment_id"
        " LEFT JOIN emails e ON e.id = a.email_id"
        f" WHERE ac.extraction_status = 'extracted' AND {_SPREADSHEET}"
    )
    args: list = []
    if attachment_ids is not None:
        sql += " AND a.id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(sorted(set(attachment_ids))))
    if new_only and has_registry(conn):
        sql += " AND a.id NOT IN (SELECT attachment_id FROM figure_editions)"
    rows = []
    for aid, filename, sha, sender, received, exported in conn.execute(sql, args):
        stem = series_stem(filename or "")
        address = (sender or "").strip().lower()
        rows.append(
            {
                "attachment_id": aid,
                "filename": filename or "",
                "sha256": sha,
                "stem": stem,
                "sender": address,
                "title": series_title(filename or "")
                if stem
                else f"Workbook named by its date, from {address or 'an unknown sender'}",
                "date": _stamp(received, exported) or "",
            }
        )
    rows.sort(key=lambda r: (r["date"], r["attachment_id"]))
    return rows


def plan_series(editions: list[dict]) -> dict[tuple[str, str], dict]:
    """The series a list of editions makes, in memory: what register_editions would store, for a
    dry run. Copies of bytes seen earlier, in any series, are not editions."""
    seen: set[str] = set()
    plan: dict[tuple[str, str], dict] = {}
    for row in editions:
        series = plan.setdefault(
            (row["stem"], row["sender"]), {"editions": 0, "copies": 0, "ids": [], "title": ""}
        )
        if row["sha256"] and row["sha256"] in seen:
            series["copies"] += 1
            continue
        if row["sha256"]:
            seen.add(row["sha256"])
        series["editions"] += 1
        series["ids"].append(row["attachment_id"])
        series["title"] = row["title"]
    return plan


def _copy_of(conn: sqlite3.Connection, sha: str | None, attachment_id: int) -> int | None:
    if not sha:
        return None
    row = conn.execute(
        "SELECT fe.attachment_id FROM attachments a"
        " JOIN figure_editions fe ON fe.attachment_id = a.id"
        " WHERE a.sha256 = ? AND a.id != ? AND fe.copy_of IS NULL"
        " ORDER BY fe.edition_date, fe.attachment_id LIMIT 1",
        (sha, attachment_id),
    ).fetchone()
    return row[0] if row else None


def register_editions(
    conn: sqlite3.Connection, attachment_ids: Iterable[int] | None = None
) -> dict[str, Any]:
    """Record the spreadsheet editions not recorded yet (of `attachment_ids`, else all), oldest
    first, each in its series. Returns the counts and the series they touched."""
    stats: dict[str, Any] = {"registered": 0, "copies": 0, "series": set()}
    for row in spreadsheet_editions(conn, attachment_ids):
        conn.execute(
            "INSERT OR IGNORE INTO figure_series (stem, sender, name) VALUES (?, ?, ?)",
            (row["stem"], row["sender"], row["title"] or row["stem"]),
        )
        series_id = conn.execute(
            "SELECT id FROM figure_series WHERE stem = ? AND sender = ?",
            (row["stem"], row["sender"]),
        ).fetchone()[0]
        copy_of = _copy_of(conn, row["sha256"], row["attachment_id"])
        conn.execute(
            "INSERT OR IGNORE INTO figure_editions (attachment_id, series_id, edition_date,"
            " copy_of, status) VALUES (?, ?, ?, ?, ?)",
            (
                row["attachment_id"],
                series_id,
                row["date"],
                copy_of,
                "copy" if copy_of else "pending",
            ),
        )
        stats["registered"] += 1
        if copy_of:
            stats["copies"] += 1
            continue
        conn.execute(
            "UPDATE figure_series SET editions = editions + 1,"
            " name = CASE WHEN last_date IS NULL OR ? >= last_date THEN ? ELSE name END,"
            " first_date = CASE WHEN first_date IS NULL OR ? < first_date THEN ? ELSE first_date END,"
            " last_date = CASE WHEN last_date IS NULL OR ? > last_date THEN ? ELSE last_date END"
            " WHERE id = ?",
            (
                row["date"],
                row["title"] or row["stem"],
                row["date"],
                row["date"],
                row["date"],
                row["date"],
                series_id,
            ),
        )
        stats["series"].add(series_id)
    conn.commit()
    stats["series"] = sorted(stats["series"])
    return stats


def parse_pending(
    conn: sqlite3.Connection,
    *,
    series_ids: Iterable[int] | None = None,
    deadline_s: float | None = None,
    limit: int | None = None,
    progress=None,
) -> dict[str, int]:
    """Parse the editions waiting in kept series, oldest first within each series, committing
    each; one that fails is marked 'failed' and the rest go on. Stops starting editions once
    `deadline_s` or `limit` is spent; what is left waits for the next run."""
    sql = (
        "SELECT fe.attachment_id FROM figure_editions fe"
        " JOIN figure_series s ON s.id = fe.series_id"
        " WHERE fe.status = 'pending' AND s.editions >= ?"
    )
    args: list = [MIN_EDITIONS]
    if series_ids is not None:
        sql += " AND s.id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(sorted(set(series_ids))))
    sql += " ORDER BY fe.series_id, fe.edition_date, fe.attachment_id"
    waiting = [r[0] for r in conn.execute(sql, args)]
    stats = {"parsed": 0, "failed": 0, "deferred": 0, "figures": 0}
    stop = None if deadline_s is None else time.monotonic() + deadline_s
    for i, attachment_id in enumerate(waiting):
        if (stop is not None and time.monotonic() >= stop) or (limit is not None and i >= limit):
            stats["deferred"] = len(waiting) - i
            break
        try:
            stats["figures"] += parse_edition(conn, attachment_id)
            stats["parsed"] += 1
        except Exception as exc:  # one odd layout must not stop the rest
            conn.rollback()
            conn.execute(
                "UPDATE figure_editions SET status = 'failed', parsed_at = ?, parser = ?"
                " WHERE attachment_id = ?",
                (utc_now(), PARSER_VERSION, attachment_id),
            )
            conn.commit()
            stats["failed"] += 1
            if progress:
                progress(f"  attachment {attachment_id}: {type(exc).__name__}: {exc}")
        if progress and (i + 1) % 100 == 0:
            progress(f"  {i + 1:,} of {len(waiting):,} editions, {stats['figures']:,} figures")
    return stats


def parse_edition(conn: sqlite3.Connection, attachment_id: int) -> int:
    """Read one edition's stored text into its series' figures; the figures stored."""
    row = conn.execute(
        "SELECT fe.series_id, fe.edition_date, a.filename, ac.extracted_text"
        " FROM figure_editions fe JOIN attachments a ON a.id = fe.attachment_id"
        " JOIN attachment_content ac ON ac.attachment_id = a.id WHERE fe.attachment_id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"attachment {attachment_id} is not a registered edition")
    series_id, edition_date, filename, text = row
    parsed = parse_workbook(text or "", filename=filename or "", email_date=edition_date)
    stored = _store(conn, series_id, attachment_id, edition_date or "", parsed.figures)
    conn.execute(
        "UPDATE figure_editions SET status = 'parsed', figures = ?, parser = ?, parsed_at = ?"
        " WHERE attachment_id = ?",
        (stored, PARSER_VERSION, utc_now(), attachment_id),
    )
    conn.commit()
    return stored


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _month_after(stamp: str) -> str:
    """The ISO day 31 days after a stamp's, or "9999-12-31" for one with no date."""
    try:
        return (date.fromisoformat(stamp[:10]) + timedelta(days=31)).isoformat()
    except ValueError:
        return "9999-12-31"


def metric_key(sheet: str, label_key: str, header_key: str) -> int:
    """The 64-bit key a metric is found by in its series: a hash of its folded sheet name,
    row label and column header, as figures_parse keys them."""
    text = "\x1f".join((sheet_key(sheet), label_key, header_key))
    return int.from_bytes(
        hashlib.blake2b(text.encode(), digest_size=8).digest(), "big", signed=True
    )


def _store(
    conn: sqlite3.Connection,
    series_id: int,
    attachment_id: int,
    edition_date: str,
    figures: list[Figure],
) -> int:
    """Write one edition's figures with supersession; returns how many it holds."""
    if not figures:
        return 0
    metrics: dict[int, int] = {
        r[1]: r[0]
        for r in conn.execute(
            "SELECT id, key FROM figure_metrics WHERE series_id = ?", (series_id,)
        )
    }
    keyed: list[tuple[int, Figure]] = []
    for fig in figures:
        key = metric_key(fig.sheet, fig.label_key, fig.header_key)
        metric_id = metrics.get(key)
        if metric_id is None:
            cursor = conn.execute(
                "INSERT INTO figure_metrics (series_id, key, sheet, label, header, unit)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (series_id, key, sheet_title(fig.sheet), fig.label, fig.header, fig.unit),
            )
            metric_id = metrics[key] = int(cursor.lastrowid or 0)
        keyed.append((metric_id, fig))
    held: dict[tuple[int, str], tuple] = {}
    for mid, period, value, aid, cell, offset, stated in conn.execute(
        "SELECT v.metric_id, v.period, v.value, v.attachment_id, v.cell, v.text_offset,"
        " fe.edition_date FROM figure_values v"
        " LEFT JOIN figure_editions fe ON fe.attachment_id = v.attachment_id"
        " WHERE v.metric_id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted({m for m, _ in keyed})),),
    ):
        held[(mid, period)] = (value, aid, cell, offset, stated or "")
    mine = (edition_date, attachment_id)
    fresh: list[tuple] = []
    moved: list[tuple] = []
    replaced: list[tuple] = []
    restated: list[tuple] = []
    # The series' latest period: the newest one ending within a month of the edition's day, when
    # it holds one; a running year's total ends on 31 December, months after the day it was
    # mailed, while the month or quarter in progress ends within one.
    day = _month_after(edition_date)
    by_day = max(((f.period_end, f.period) for _, f in keyed if f.period_end <= day), default=None)
    latest_end, latest_period = by_day or max((f.period_end, f.period) for _, f in keyed)
    for metric_id, fig in keyed:
        where = (metric_id, fig.period_end, fig.period)
        current = held.get((metric_id, fig.period))
        if current is None:
            fresh.append(
                (*where, fig.period_source, fig.value, attachment_id, fig.cell, fig.offset)
            )
            held[(metric_id, fig.period)] = (
                fig.value,
                attachment_id,
                fig.cell,
                fig.offset,
                edition_date,
            )
            continue
        value, aid, cell, offset, stated = current
        if aid == attachment_id:
            continue
        newer = mine > (stated, aid)
        if _same(value, fig.value):
            if newer:  # the newest edition stating it is the one cited
                moved.append((attachment_id, fig.cell, fig.offset, fig.period_source, *where))
                held[(metric_id, fig.period)] = (
                    value,
                    attachment_id,
                    fig.cell,
                    fig.offset,
                    edition_date,
                )
        elif newer:  # restated: the newer value is held, the older one kept apart
            restated.append((metric_id, fig.period, value, aid, cell, offset))
            replaced.append(
                (fig.value, attachment_id, fig.cell, fig.offset, fig.period_source, *where)
            )
            held[(metric_id, fig.period)] = (
                fig.value,
                attachment_id,
                fig.cell,
                fig.offset,
                edition_date,
            )
        else:  # an older edition's value for a period a newer one restated
            restated.append((metric_id, fig.period, fig.value, attachment_id, fig.cell, fig.offset))
    conn.executemany(
        "INSERT INTO figure_values (metric_id, period_end, period, period_source, value,"
        " attachment_id, cell, text_offset) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        fresh,
    )
    at = " WHERE metric_id = ? AND period_end = ? AND period = ?"
    conn.executemany(
        "UPDATE figure_values SET attachment_id = ?, cell = ?, text_offset = ?, period_source = ?"
        + at,
        moved,
    )
    conn.executemany(
        "UPDATE figure_values SET value = ?, attachment_id = ?, cell = ?, text_offset = ?,"
        " period_source = ?" + at,
        replaced,
    )
    conn.executemany(
        "INSERT INTO figure_restated (metric_id, period, value, attachment_id, cell, text_offset)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        restated,
    )
    # Set by the newest edition read so far, whatever order the editions are read in.
    conn.execute(
        "UPDATE figure_series SET latest_period = ?, latest_end = ?, latest_from = ?"
        " WHERE id = ? AND (latest_from IS NULL OR latest_from <= ?)",
        (latest_period, latest_end, edition_date, series_id, edition_date),
    )
    return len(keyed)


def ingest_editions(
    conn: sqlite3.Connection,
    attachment_ids: Iterable[int],
    *,
    deadline_s: float = INGEST_DEADLINE_S,
) -> dict[str, int]:
    """The hourly hook: record the new spreadsheet editions among `attachment_ids` and parse the
    waiting editions of the series they belong to, in `deadline_s` at most. The backlog of
    series not touched waits for `brain maint figures-backfill`."""
    ids = list(attachment_ids)
    if not ids or not has_registry(conn):
        return {}
    registered = register_editions(conn, ids)
    if not registered["registered"]:
        return {}
    if not registered["series"]:  # copies only
        return {"registered": registered["registered"]}
    stats = parse_pending(conn, series_ids=registered["series"], deadline_s=deadline_s)
    return {"registered": registered["registered"], **stats}


def reset(conn: sqlite3.Connection) -> None:
    """Forget every figure, metric, edition and series, for a backfill to start again (a
    parser change). Metrics go before their series: the index's delete reads the series' stem."""
    for table in (
        "figure_restated",
        "figure_values",
        "figure_metrics",
        "figure_editions",
        "figure_series",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


# ------------------------------------------------------------------ reading


def _value(row: sqlite3.Row | tuple | None) -> dict | None:
    if row is None:
        return None
    period, period_end, source, value, aid, cell, offset, edition_date = row
    return {
        "value": value,
        "period": period,
        "period_end": period_end,
        "period_source": source,
        "edition_date": edition_date,
        "source": f"attachment:{aid}",
        "attachment_id": aid,
        "cell": cell,
        "offset": offset,
    }


_VALUE = (
    "SELECT v.period, v.period_end, v.period_source, v.value, v.attachment_id, v.cell,"
    " v.text_offset, fe.edition_date FROM figure_values v"
    " LEFT JOIN figure_editions fe ON fe.attachment_id = v.attachment_id"
    " WHERE v.metric_id = ?"
)
_NEWEST = " ORDER BY v.period_end DESC, v.period DESC"


def _asked(period: str | None) -> tuple[str, str, str] | None:
    """(label, first day, last day) of the period a caller asked for."""
    if period is None or not period.strip():
        return None
    found = parse_period(period.strip())
    bounds = period_bounds(found.period.label) if found and not found.rest else None
    if found is None or bounds is None:
        raise ValueError(
            "period must name a day, month, quarter, half or year (2026-08-31, 2026-08, "
            f"August 2026, 2026-Q3, 2026-H1, 2026); got {period!r}"
        )
    return found.period.label, bounds[0], bounds[1]


def _series_ids(conn: sqlite3.Connection, series: str | None) -> list[int] | None:
    """The kept series a `series` filter means: an id, or every series whose name holds its
    words; None for no filter."""
    if series is None or not str(series).strip():
        return None
    text = str(series).strip()
    if text.isdigit():
        return [int(text)]
    words = search_words(text)
    ids = []
    for sid, name, stem in conn.execute(
        "SELECT id, name, stem FROM figure_series WHERE editions >= ?", (MIN_EDITIONS,)
    ):
        folded = f"{search_fold(name)} {stem}"
        if words and all(w in folded for w in words):
            ids.append(sid)
    return ids


def _candidates(
    conn: sqlite3.Connection, query: str, series_ids: list[int] | None
) -> tuple[list[tuple[int, float]], bool]:
    """Metrics matching the query, best first, and whether they hold only some of its words."""
    from src.store.query import fts5_query_variants

    where = ""
    args: list = []
    if series_ids is not None:
        where = " AND m.series_id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(series_ids))
    for expr, partial in fts5_query_variants(query):
        rows = conn.execute(
            "SELECT f.rowid, bm25(figure_metrics_fts, 1.0, 1.0, 2.0, 2.0) AS score"
            " FROM figure_metrics_fts f JOIN figure_metrics m ON m.id = f.rowid"
            " JOIN figure_series s ON s.id = m.series_id"
            f" WHERE figure_metrics_fts MATCH ? AND s.editions >= ?{where}"
            " ORDER BY score LIMIT ?",
            [expr, MIN_EDITIONS, *args, CANDIDATES],
        ).fetchall()
        if rows:
            return [(r[0], r[1]) for r in rows], partial
    return [], False


def _metric_name(label: str, header: str) -> str:
    return " / ".join(part for part in (label, header) if part) or "(unnamed)"


def search_figures(
    conn: sqlite3.Connection,
    query: str,
    *,
    period: str | None = None,
    series: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """The metrics matching `query` (plain words, Greek or English), each with its latest held
    value, the period before it, and the edition, sheet and cell each came from; with `period`,
    the value held for that period, or `held: false` and the latest held. Newest period first,
    rows holding every word before rows holding some. `reason` says why a list is short."""
    if not has_registry(conn):
        return {"result": [], "reason": NOT_BUILT}
    register_sql_functions(conn)
    asked = _asked(period)
    limit = max(1, min(int(limit), MAX_LIMIT))
    series_ids = _series_ids(conn, series)
    if series_ids == []:
        return {
            "result": [],
            "reason": f"No kept series is named like {series!r}: figures(mode='series') lists them.",
        }
    found, partial = _candidates(conn, query, series_ids)
    if not found:
        return {
            "result": [],
            "reason": "No figure matched these words in a row label, column header, sheet or "
            "series name. Try fewer or other words, the other language, or "
            "figures(mode='series') for the reports held.",
        }
    meta = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT m.id, m.series_id, s.name, m.sheet, m.label, m.header, m.unit"
            " FROM figure_metrics m JOIN figure_series s ON s.id = m.series_id"
            " WHERE m.id IN (SELECT value FROM json_each(?))",
            (json.dumps([mid for mid, _ in found]),),
        )
    }
    rows = []
    for metric_id, _score in found:
        if metric_id not in meta:
            continue
        series_id, name, sheet, label, header, unit = meta[metric_id]
        latest = _value(conn.execute(_VALUE + _NEWEST + " LIMIT 1", (metric_id,)).fetchone())
        if latest is None:
            continue
        row: dict[str, Any] = {
            "metric": _metric_name(label, header),
            "series": name,
            "series_id": series_id,
            "sheet": sheet,
            "label": label,
            "header": header,
            "unit": unit,
        }
        if asked is not None:
            label_asked, first, last = asked
            inside = [
                v
                for v in map(
                    _value,
                    conn.execute(
                        _VALUE + " AND v.period_end BETWEEN ? AND ?" + _NEWEST,
                        (metric_id, first, last),
                    ),
                )
                if v is not None and (period_bounds(v["period"]) or ("", ""))[0] >= first
            ]
            row["held"] = bool(inside)
            if inside:
                latest = inside[0]
            else:
                row["reason"] = f"{label_asked} not held; latest held: {latest['period']}"
        previous = _value(
            conn.execute(
                _VALUE + " AND v.period_end < ?" + _NEWEST + " LIMIT 1",
                (metric_id, latest["period_end"]),
            ).fetchone()
        )
        restated = conn.execute(
            "SELECT r.value, r.attachment_id FROM figure_restated r"
            " LEFT JOIN figure_editions fe ON fe.attachment_id = r.attachment_id"
            " WHERE r.metric_id = ? AND r.period = ? ORDER BY fe.edition_date DESC, r.id DESC"
            " LIMIT 1",
            (metric_id, latest["period"]),
        ).fetchone()
        row["latest"] = latest
        row["previous"] = previous
        row["restated_from"] = (
            {"value": restated[0], "source": f"attachment:{restated[1]}"} if restated else None
        )
        row["attachment_id"] = latest["attachment_id"]
        rows.append(row)
    # Best match first, then (stable) newest period first, then rows holding the asked period.
    rows.sort(key=lambda r: r["latest"]["period_end"], reverse=True)
    rows.sort(key=lambda r: not r.get("held", True))
    rows = rows[:limit]
    for row in rows:
        if partial:
            row["partial_match"] = True
    envelope.attachments(conn, rows)
    out: dict[str, Any] = {"result": rows}
    if asked is not None and rows and not any(r.get("held") for r in rows):
        newest = max(rows, key=lambda r: r["latest"]["period_end"])["latest"]["period"]
        out["reason"] = f"{asked[0]} not held; latest held: {newest}"
    return out


def list_series(
    conn: sqlite3.Connection, query: str | None = None, limit: int = MAX_LIMIT
) -> list[dict]:
    """The kept series, newest edition first: name, sender, editions, the dates of the first
    and the last, the latest period held, how many metrics, and the newest edition's envelope.
    `query` keeps those whose name holds its words."""
    if not has_registry(conn):
        return []
    words = search_words(query) if query else []
    rows = []
    for sid, name, stem, sender, editions, first, last, latest in conn.execute(
        "SELECT id, name, stem, sender, editions, first_date, last_date, latest_period"
        " FROM figure_series WHERE editions >= ? AND latest_period IS NOT NULL"
        " ORDER BY last_date DESC, id DESC",
        (MIN_EDITIONS,),
    ):
        if words and not all(w in f"{search_fold(name)} {stem}" for w in words):
            continue
        newest = conn.execute(
            "SELECT attachment_id FROM figure_editions WHERE series_id = ? AND copy_of IS NULL"
            " ORDER BY edition_date DESC, attachment_id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        metrics = conn.execute(
            "SELECT COUNT(*) FROM figure_metrics WHERE series_id = ?", (sid,)
        ).fetchone()[0]
        rows.append(
            {
                "series": name,
                "series_id": sid,
                "sender": sender,
                "editions": editions,
                "first_edition": first,
                "last_edition": last,
                "latest_period": latest,
                "metrics": metrics,
                "attachment_id": newest[0] if newest else None,
            }
        )
        if len(rows) >= max(1, min(int(limit), MAX_LIMIT)):
            break
    return envelope.attachments(conn, rows)


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the registry holds: kept series, their editions, the first and last edition date,
    and the editions waiting to be read."""
    if not has_registry(conn):
        return {"series": 0, "editions": 0, "first": None, "last": None, "waiting": 0}
    series, editions, first, last = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(editions), 0), MIN(first_date), MAX(last_date)"
        " FROM figure_series WHERE editions >= ?",
        (MIN_EDITIONS,),
    ).fetchone()
    waiting = conn.execute(
        "SELECT COUNT(*) FROM figure_editions fe JOIN figure_series s ON s.id = fe.series_id"
        " WHERE fe.status = 'pending' AND s.editions >= ?",
        (MIN_EDITIONS,),
    ).fetchone()[0]
    return {
        "series": series,
        "editions": editions,
        "first": first,
        "last": last,
        "waiting": waiting,
    }


# ------------------------------------------------------------------ the backfill

# Editions --estimate parses, spread over the kept series, to project figures and time.
ESTIMATE_SAMPLE = 100
# A backfill takes about this many times its parsing alone: on a copy of a replica's corpus,
# 92 s for 4,548 editions, of which parsing was 33 s.
STORE_FACTOR = 2.8


def _plan(conn: sqlite3.Connection) -> dict[str, Any]:
    """The series every spreadsheet edition held makes, and what waits: read, never written."""
    editions = spreadsheet_editions(conn, new_only=False)
    plan = plan_series(editions)
    kept = {k: v for k, v in plan.items() if v["editions"] >= MIN_EDITIONS}
    ids = [i for series in kept.values() for i in series["ids"]]
    done: set[int] = set()
    if has_registry(conn):
        done = {
            r[0]
            for r in conn.execute(
                "SELECT attachment_id FROM figure_editions WHERE status != 'pending'"
            )
        }
    waiting = [i for i in ids if i not in done]
    chars = 0
    for start in range(0, len(waiting), 500):
        chars += conn.execute(
            "SELECT COALESCE(SUM(length(extracted_text)), 0) FROM attachment_content"
            " WHERE attachment_id IN (SELECT value FROM json_each(?))",
            (json.dumps(waiting[start : start + 500]),),
        ).fetchone()[0]
    return {
        "editions": len(editions),
        "copies": sum(s["copies"] for s in plan.values()),
        "series": len(plan),
        "kept": len(kept),
        "kept_editions": len(ids),
        "done": len(ids) - len(waiting),
        "waiting": waiting,
        "chars": chars,
    }


def _estimate(conn: sqlite3.Connection, waiting: list[int]) -> dict[str, float]:
    """Parse a spread of the waiting editions, storing nothing, and project the figures and the
    seconds per edition over every waiting one: per edition, not per character, as the longest
    texts are mostly sheets too long to read, passed over in milliseconds."""
    if not waiting:
        return {"sampled": 0, "figures": 0, "seconds": 0.0}
    step = max(1, len(waiting) // ESTIMATE_SAMPLE)
    sample = waiting[::step][:ESTIMATE_SAMPLE]
    found = 0
    started = time.perf_counter()
    for attachment_id in sample:
        row = conn.execute(
            "SELECT a.filename, ac.extracted_text, e.date_received FROM attachments a"
            " JOIN attachment_content ac ON ac.attachment_id = a.id"
            " LEFT JOIN emails e ON e.id = a.email_id WHERE a.id = ?",
            (attachment_id,),
        ).fetchone()
        if row is not None:
            parsed = parse_workbook(row[1] or "", filename=row[0] or "", email_date=row[2])
            found += len(parsed.figures)
    spent = time.perf_counter() - started
    scale = len(waiting) / len(sample)
    return {"sampled": len(sample), "figures": found * scale, "seconds": spent * scale}


def cmd(args) -> int:
    """`brain maint figures-backfill`: a dry run unless --apply; --estimate parses a sample."""
    from src.config import is_replica, replica_refusal
    from src.store.schema import get_connection, run_migrations
    from src.store.sql_readonly import connect_read_only

    if args.apply and is_replica():
        print(replica_refusal("figures-backfill --apply"))
        return 3
    if args.reset and not args.apply:
        print("--reset forgets every figure, so it runs only with --apply")
        return 2
    if not args.apply:
        conn = connect_read_only(args.db, immutable=True)
        try:
            plan = _plan(conn)
            estimate = _estimate(conn, plan["waiting"]) if args.estimate else None
        finally:
            conn.close()
        print("figures-backfill DRY RUN (nothing written):")
        print(
            f"  {'spreadsheet editions held':<34}: {plan['editions']:,} ({plan['copies']:,} copies)"
        )
        print(f"  {'report series (stem and sender)':<34}: {plan['series']:,}")
        print(
            f"  {'kept series (3+ editions)':<34}: {plan['kept']:,}, {plan['kept_editions']:,} editions"
        )
        print(f"  {'already read':<34}: {plan['done']:,}")
        print(f"  {'waiting':<34}: {len(plan['waiting']):,}, {plan['chars']:,} characters")
        if estimate is not None:
            print(
                f"  {'estimate, from ' + str(estimate['sampled']) + ' editions':<34}: "
                f"~{estimate['figures']:,.0f} figures, ~{estimate['seconds'] * STORE_FACTOR / 60:,.1f} min"
                " on this host, storing included"
            )
        print("Run it again with --apply to read them; it resumes where a run stopped.")
        return 0
    conn = get_connection(str(args.db))
    try:
        run_migrations(conn)
        if args.reset:
            reset(conn)
        registered = register_editions(conn)
        started = time.perf_counter()
        stats = parse_pending(conn, limit=args.limit, progress=print)
        spent = time.perf_counter() - started
        held = coverage(conn)
        values = conn.execute("SELECT COUNT(*) FROM figure_values").fetchone()[0]
        metrics = conn.execute("SELECT COUNT(*) FROM figure_metrics").fetchone()[0]
    finally:
        conn.close()
    print(f"figures-backfill APPLIED in {spent / 60:.1f} min:")
    print(
        f"  {'editions recorded':<26}: {registered['registered']:,} ({registered['copies']:,} copies)"
    )
    print(f"  {'editions read':<26}: {stats['parsed']:,}, {stats['failed']:,} failed")
    print(f"  {'figures read':<26}: {stats['figures']:,}")
    print(f"  {'held':<26}: {metrics:,} metrics, {values:,} values, {held['series']:,} series")
    if stats["deferred"]:
        print(f"  {'still waiting':<26}: {stats['deferred']:,} (--limit); run it again")
    return 1 if stats["failed"] and not stats["parsed"] else 0

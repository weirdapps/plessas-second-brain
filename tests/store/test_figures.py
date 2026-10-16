"""The numbers registry: report series, their editions, and the figures each one holds.

Editions of a recurring report are grouped by the stem of their filename and their sender; a
series is kept from its third edition on. Each kept edition's stored dump is parsed into
figures (src/extract/figures_parse.py), and for one metric (series, sheet, row label, column
header) a newer period supersedes an older one, and a newer edition restating a period
supersedes the older edition's value. `search_figures` answers with the latest held value of
each metric, the one before it, and the sheet and cell it was read from.
"""

import sqlite3
from datetime import datetime

import openpyxl
import pytest

from src.extract.attachment_extractors import extract_text_from_file
from src.store import figures
from src.store.schema import create_database
from tests import factories as f

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REPORTS = ("Reports Desk", "reports@example.com")


@pytest.fixture
def conn(tmp_path):
    connection = create_database(str(tmp_path / "brain.db"))
    yield connection
    connection.close()


def _dump(tmp_path, rows: list[list], sheet: str = "KPIs") -> str:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    path = tmp_path / f"book-{len(list(tmp_path.iterdir()))}.xlsx"
    wb.save(path)
    return extract_text_from_file(str(path), XLSX)["text"]


def _edition(
    conn,
    tmp_path,
    filename: str,
    rows: list[list],
    date: str,
    *,
    sender=REPORTS,
    sha=None,
    sheet="KPIs",
) -> int:
    email_id = f.email(conn, subject=f"Report {filename}", sender=sender, date_received=date)
    return f.attachment(
        conn,
        email_id=email_id,
        filename=filename,
        text=_dump(tmp_path, rows, sheet),
        sha256=sha or f"sha-{filename}-{date}",
        content_columns={"extraction_method": "openpyxl"},
    )


def _monthly(conn, tmp_path, month: int, loans: float, *, filename=None, restated=None):
    """An edition of a monthly report: one column per month up to `month` of 2026."""
    months = [datetime(2026, m, 28) for m in range(1, month + 1)]
    values = [restated.get(m, 100 + m) if restated else 100 + m for m in range(1, month)]
    rows = [
        ["Product", *months],
        ["Consumer loans", *values, loans],
        ["Κάρτες", *range(1, month + 1)],
    ]
    name = filename or f"Digital sales 2026-{month:02d}.xlsx"
    return _edition(conn, tmp_path, name, rows, f"2026-{month:02d}-28T08:00:00Z")


def _built(conn) -> dict:
    stats = figures.register_editions(conn)
    stats.update(figures.parse_pending(conn))
    return stats


# ------------------------------------------------------------------ series


def test_a_series_is_kept_from_its_third_edition(conn, tmp_path):
    _monthly(conn, tmp_path, 7, 0.70)
    _monthly(conn, tmp_path, 8, 0.72)
    figures.register_editions(conn)
    assert figures.parse_pending(conn)["parsed"] == 0

    _monthly(conn, tmp_path, 9, 0.73)
    figures.register_editions(conn)
    stats = figures.parse_pending(conn)

    assert stats["parsed"] == 3
    (series,) = figures.list_series(conn)
    assert (series["series"], series["editions"]) == ("Digital sales", 3)


def test_a_copy_of_an_edition_is_no_new_edition(conn, tmp_path):
    first = _monthly(conn, tmp_path, 7, 0.70)
    _monthly(conn, tmp_path, 8, 0.72)
    sha = conn.execute("SELECT sha256 FROM attachments WHERE id = ?", (first,)).fetchone()[0]
    rows = [["Product", datetime(2026, 7, 28)], ["Consumer loans", 0.70]]
    _edition(
        conn, tmp_path, "Copy of Digital sales 2026-07.xlsx", rows, "2026-08-30T08:00:00Z", sha=sha
    )

    _built(conn)

    assert figures.list_series(conn) == []
    status = conn.execute(
        "SELECT status FROM figure_editions WHERE copy_of = ?", (first,)
    ).fetchall()
    assert [r[0] for r in status] == ["copy"]


def test_the_same_report_from_another_sender_is_another_series(conn, tmp_path):
    for month in (7, 8):
        _monthly(conn, tmp_path, month, 0.7)
    rows = [["Product", datetime(2026, 9, 28)], ["Consumer loans", 0.73]]
    _edition(
        conn, tmp_path, "Digital sales 2026-09.xlsx", rows, "2026-09-28T08:00:00Z", sender=f.BOB
    )

    _built(conn)

    assert figures.list_series(conn) == []


def test_a_sheet_named_by_its_date_is_one_sheet_in_every_edition(conn, tmp_path):
    for day in (13, 14, 15):
        rows = [["Line", "Entered"], ["Retail line", 1900 + day]]
        name = f"Call centre {day}.10.2026.xlsx"
        _edition(conn, tmp_path, name, rows, f"2026-10-{day}T09:00:00Z", sheet=f"{day}.10.2026")
    _built(conn)

    (row,) = figures.search_figures(conn, "retail line")["result"]

    assert row["sheet"] == "(sheet named by its date)"
    assert (row["latest"]["period"], row["latest"]["period_source"]) == ("2026-10-15", "sheet")
    assert row["previous"]["value"] == 1914


def test_a_running_years_total_does_not_stand_for_the_series_latest_period(conn, tmp_path):
    for day in (8, 9, 10):
        rows = [
            ["Metric", datetime(2026, 9, day - 1), datetime(2026, 9, day), "Total 2026"],
            ["Payments", 5, 6, 900],
        ]
        name = f"Payments 2026-09-{day:02d}.xlsx"
        _edition(conn, tmp_path, name, rows, f"2026-09-{day:02d}T09:00:00Z")
    _built(conn)

    (series,) = figures.list_series(conn)

    assert series["latest_period"] == "2026-09-10"


def test_a_series_whose_filenames_are_only_dates_is_named_by_its_sender(conn, tmp_path):
    for day in (1, 2, 3):
        rows = [["Line", "Entered"], ["Retail line", day]]
        _edition(conn, tmp_path, f"0{day}-04-2026.xlsx", rows, f"2026-04-0{day}T09:00:00Z")
    _built(conn)

    (series,) = figures.list_series(conn)

    assert series["series"] == "Workbook named by its date, from reports@example.com"


def _matches(conn, word: str) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT rowid FROM figure_metrics_fts WHERE figure_metrics_fts MATCH ?", (word,)
        )
    ]


def test_the_index_follows_its_metrics_and_survives_a_rebuild(conn, tmp_path):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
    _built(conn)
    loans = conn.execute("SELECT id FROM figure_metrics WHERE label = 'Consumer loans'").fetchone()[
        0
    ]

    conn.execute("UPDATE figure_metrics SET unit = '%' WHERE id = ?", (loans,))
    conn.execute("UPDATE figure_metrics SET label = 'Personal loans' WHERE id = ?", (loans,))
    conn.commit()
    assert _matches(conn, "personal") == [loans] and _matches(conn, "consumer") == []

    conn.execute("INSERT INTO figure_metrics_fts(figure_metrics_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO figure_metrics_fts(figure_metrics_fts) VALUES('integrity-check')")
    assert _matches(conn, "personal") == [loans]
    assert _matches(conn, "digital") != []  # the series' stem is indexed too

    figures.reset(conn)
    conn.execute("INSERT INTO figure_metrics_fts(figure_metrics_fts) VALUES('integrity-check')")
    assert _matches(conn, "personal") == []


# ------------------------------------------------------------------ ranking


def _ranking_store(conn, tmp_path):
    """A monthly migration index with English product headers, and a newer daily call-centre
    report whose lines include one on loans."""
    for month in (6, 7, 8):
        months = [datetime(2026, m, 1) for m in range(1, month + 1)]
        rows = [["Digital migration index"], ["Month", "Consumer Loans (5)", "Debit Cards (1)"]]
        rows += [[d, 0.70 + d.month / 100, 0.30] for d in months]
        _edition(
            conn,
            tmp_path,
            f"Δείκτης Μετάπτωσης 2026-{month:02d}.xlsx",
            rows,
            f"2026-{month + 1:02d}-20T08:00:00Z",
        )
    for day in (13, 14, 15):
        rows = [["Line", "Calls"], ["Line B Δάνεια", 100 + day], ["Line B Κάρτες", 50 + day]]
        _edition(
            conn, tmp_path, f"Call centre {day}.10.2026.xlsx", rows, f"2026-10-{day}T09:00:00Z"
        )
    _built(conn)


def test_a_full_match_outranks_a_newer_partial_one(conn, tmp_path):
    _ranking_store(conn, tmp_path)

    rows = figures.search_figures(conn, "μετάπτωση καταναλωτικά δάνεια")["result"]

    assert rows[0]["header"] == "Consumer Loans (5)"
    assert rows[0]["latest"]["period"] == "2026-08" and "partial_match" not in rows[0]
    loans = [r for r in rows if r["label"] == "Line B Δάνεια"]
    assert loans and loans[0]["partial_match"] is True
    assert rows.index(loans[0]) > 0


def test_more_matched_words_rank_first_among_partial_matches(conn, tmp_path):
    _ranking_store(conn, tmp_path)

    rows = figures.search_figures(conn, "μετάπτωση κάρτες δάνεια")["result"]

    assert {r["header"] for r in rows[:2]} == {"Consumer Loans (5)", "Debit Cards (1)"}
    assert all(r.get("partial_match") for r in rows)
    assert rows[-1]["label"].startswith("Line B")


def test_a_running_years_target_does_not_outrank_this_months_figure(conn, tmp_path):
    """A year's figure ends on 31 December; mailed in January it is as recent as January."""
    _ranking_store(conn, tmp_path)
    for day in (12, 13, 14):
        rows = [["Product", "Target"], ["Consumer loans", 900]]
        _edition(conn, tmp_path, f"Targets 2026 v{day}.xlsx", rows, f"2026-01-{day}T09:00:00Z")
    _built(conn)

    rows = figures.search_figures(conn, "consumer loans")["result"]

    assert rows[0]["header"] == "Consumer Loans (5)"
    assert any(r["series"] == "Targets" for r in rows)


def test_greek_forms_and_report_terms_in_english_both_match(conn, tmp_path):
    _ranking_store(conn, tmp_path)

    rows = figures.search_figures(conn, "δανείων")["result"]

    assert {r["header"] for r in rows} >= {"Consumer Loans (5)"}
    assert {r["label"] for r in rows} >= {"Line B Δάνεια"}
    assert not any(r.get("partial_match") for r in rows)


# ------------------------------------------------------------------ supersession


def test_the_latest_held_is_the_newest_period_with_its_edition_and_cell(conn, tmp_path):
    _monthly(conn, tmp_path, 7, 0.70)
    _monthly(conn, tmp_path, 8, 0.72)
    september = _monthly(conn, tmp_path, 9, 0.73)
    _built(conn)

    out = figures.search_figures(conn, "consumer loans")

    (row,) = out["result"]
    assert row["latest"]["value"] == 0.73
    assert row["latest"]["period"] == "2026-09"
    assert row["latest"]["source"] == f"attachment:{september}"
    assert row["latest"]["cell"].startswith("R2C")
    assert row["previous"]["value"] == 108
    assert row["previous"]["period"] == "2026-08"
    assert row["kind"] == "attachment" and row["ref"] == september


def test_a_newer_edition_restating_a_period_supersedes_the_older_value(conn, tmp_path):
    _monthly(conn, tmp_path, 7, 0.70)
    _monthly(conn, tmp_path, 8, 0.72)
    # September restates August: 0.75, not 0.72.
    september = _monthly(conn, tmp_path, 9, 0.73, restated={8: 0.75})
    _built(conn)

    out = figures.search_figures(conn, "consumer loans", period="2026-08")

    (row,) = out["result"]
    assert row["held"] is True
    assert row["latest"]["value"] == 0.75
    assert row["latest"]["source"] == f"attachment:{september}"
    assert row["restated_from"]["value"] == 0.72
    loans = "(SELECT id FROM figure_metrics WHERE label = 'Consumer loans')"
    held = conn.execute(
        f"SELECT value FROM figure_values WHERE period = '2026-08' AND metric_id IN {loans}"
    ).fetchall()
    kept = conn.execute(
        f"SELECT value FROM figure_restated WHERE period = '2026-08' AND metric_id IN {loans}"
    ).fetchall()
    assert [tuple(r) for r in held] == [(0.75,)]
    assert [tuple(r) for r in kept] == [(0.72,)]


def test_an_unchanged_restatement_points_at_the_newest_edition_and_adds_no_row(conn, tmp_path):
    _monthly(conn, tmp_path, 7, 0.70)
    _monthly(conn, tmp_path, 8, 0.72)
    september = _monthly(conn, tmp_path, 9, 0.73)
    _built(conn)

    # June is 106 in all three editions.
    june = conn.execute(
        "SELECT attachment_id FROM figure_values v JOIN figure_metrics m ON m.id = v.metric_id"
        " WHERE m.label = 'Consumer loans' AND v.period = '2026-06'"
    ).fetchall()

    assert [tuple(r) for r in june] == [(september,)]


def test_an_older_edition_parsed_late_does_not_override_a_newer_one(conn, tmp_path):
    _monthly(conn, tmp_path, 8, 0.72)
    _monthly(conn, tmp_path, 9, 0.73, restated={8: 0.75})
    october = _monthly(conn, tmp_path, 10, 0.74, restated={8: 0.75})
    figures.register_editions(conn)
    # Parse the newest first: the August edition comes last.
    order = conn.execute(
        "SELECT attachment_id FROM figure_editions ORDER BY edition_date DESC"
    ).fetchall()
    for (attachment_id,) in order:
        figures.parse_edition(conn, attachment_id)

    out = figures.search_figures(conn, "consumer loans", period="2026-08")

    assert out["result"][0]["latest"]["value"] == 0.75
    assert out["result"][0]["latest"]["source"] == f"attachment:{october}"
    assert out["result"][0]["restated_from"]["value"] == 0.72


# ------------------------------------------------------------------ search


def test_search_folds_greek_accents_and_finds_other_forms(conn, tmp_path):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
    _built(conn)

    for query in ("κάρτες", "καρτες", "ΚΑΡΤΩΝ", "digital sales κάρτες"):
        rows = figures.search_figures(conn, query)["result"]
        full = [r["label"] for r in rows if not r.get("partial_match")]
        # The rows holding only some words ("digital sales") come after, flagged.
        assert full == ["Κάρτες"] and rows[0]["label"] == "Κάρτες", query


def test_a_period_not_held_says_so_and_names_the_latest_held(conn, tmp_path):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
    _built(conn)

    out = figures.search_figures(conn, "consumer loans", period="October 2026")

    assert out["reason"] == "2026-10 not held; latest held: 2026-09"
    (row,) = out["result"]
    assert row["held"] is False and row["latest"]["period"] == "2026-09"


def test_an_unreadable_period_is_refused(conn, tmp_path):
    with pytest.raises(ValueError, match="period"):
        figures.search_figures(conn, "consumer loans", period="soon")


def test_the_series_filter_takes_a_name_or_an_id(conn, tmp_path):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
        _monthly(conn, tmp_path, month, 0.1, filename=f"Branch volumes 2026-{month:02d}.xlsx")
    _built(conn)
    series = {s["series"]: s["series_id"] for s in figures.list_series(conn)}

    by_name = figures.search_figures(conn, "consumer loans", series="branch volumes")["result"]
    by_id = figures.search_figures(conn, "consumer loans", series=str(series["Digital sales"]))

    assert {r["series"] for r in by_name} == {"Branch volumes"}
    assert {r["series"] for r in by_id["result"]} == {"Digital sales"}


def test_nothing_matched_says_why(conn, tmp_path):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
    _built(conn)

    out = figures.search_figures(conn, "okapi sightings")

    assert out["result"] == [] and "No figure" in out["reason"]


def test_a_store_without_the_registry_says_so(tmp_path):
    bare = sqlite3.connect(":memory:")

    out = figures.search_figures(bare, "consumer loans")

    assert out["result"] == [] and "figures-backfill" in out["reason"]


def test_series_listing_newest_first_with_its_latest_period(conn, tmp_path):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
    _built(conn)

    (series,) = figures.list_series(conn)

    assert series["latest_period"] == "2026-09"
    assert series["last_edition"].startswith("2026-09-28")
    assert series["metrics"] == 2
    assert series["kind"] == "attachment"


def test_a_failed_parse_marks_the_edition_and_goes_on(conn, tmp_path, monkeypatch):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
    figures.register_editions(conn)
    real = figures.parse_workbook
    calls = []

    def flaky(text, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("layout nobody expected")
        return real(text, **kwargs)

    monkeypatch.setattr(figures, "parse_workbook", flaky)

    stats = figures.parse_pending(conn)

    assert (stats["parsed"], stats["failed"]) == (2, 1)
    assert (
        conn.execute("SELECT COUNT(*) FROM figure_editions WHERE status = 'failed'").fetchone()[0]
        == 1
    )


def test_parse_pending_stops_at_its_deadline_and_resumes(conn, tmp_path):
    for month in (7, 8, 9):
        _monthly(conn, tmp_path, month, 0.7)
    figures.register_editions(conn)

    first = figures.parse_pending(conn, deadline_s=0)
    second = figures.parse_pending(conn)

    assert (first["parsed"], first["deferred"]) == (0, 3)
    assert second["parsed"] == 3

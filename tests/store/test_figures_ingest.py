"""The hourly text phase reads the figures of new report editions, cheaply, and never fails for them.

Phase 1 stores a workbook's text; when that workbook is an edition of a kept series (its third
edition or later), its figures are read from that text in the same run, in a time box of its own
(figures.INGEST_DEADLINE_S), and only for the series it touched: the backlog is the backfill's.
"""

from datetime import datetime

import openpyxl
import pytest

from src.extract.attachment_pipeline import run_phase1
from src.store import figures
from src.store.schema import create_database
from tests import factories as f

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _workbook(path, month: int, rows: int = 12) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "KPIs"
    ws.append(["Product", *[datetime(2026, m, 28) for m in range(1, month + 1)]])
    for r in range(rows):
        ws.append([f"Product {r}", *[r * 10 + m for m in range(1, month + 1)]])
    wb.save(path)


def _mail(conn, tmp_path, month: int, rows: int = 12) -> int:
    name = f"Volumes 2026-{month:02d}.xlsx"
    path = tmp_path / name
    _workbook(path, month, rows)
    email_id = f.email(
        conn,
        subject=name,
        sender=("Reports", "reports@example.com"),
        date_received=f"2026-{month:02d}-28T08:00:00Z",
    )
    return f.attachment(
        conn,
        email_id=email_id,
        filename=name,
        text=None,
        file_path=str(path),
        mime_type=XLSX,
        sha256=f"sha-{month}",
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    return path


def _conn(db):
    from src.store.schema import get_connection

    return get_connection(str(db))


def test_the_third_edition_brings_its_series_figures_in_the_text_phase(db, tmp_path):
    conn = _conn(db)
    for month in (7, 8):
        _mail(conn, tmp_path, month)
    conn.close()
    first = run_phase1(str(db))
    assert (first["figures"]["registered"], first["figures"]["parsed"]) == (2, 0)

    conn = _conn(db)
    _mail(conn, tmp_path, 9)
    conn.close()
    third = run_phase1(str(db))

    assert third["figures"]["parsed"] == 3
    conn = _conn(db)
    out = figures.search_figures(conn, "product 3")
    assert out["result"][0]["latest"]["period"] == "2026-09"
    conn.close()


def test_a_failing_registry_never_fails_the_text_phase(db, tmp_path, monkeypatch, caplog):
    conn = _conn(db)
    _mail(conn, tmp_path, 7)
    conn.close()

    def broken(*args, **kwargs):
        raise RuntimeError("registry down")

    monkeypatch.setattr(figures, "ingest_editions", broken)

    stats = run_phase1(str(db))

    assert stats["extracted"] == 1 and stats["figures"] == {"error": 1}
    assert "figures" in caplog.text


def test_text_that_is_not_a_workbook_adds_nothing(db, tmp_path):
    conn = _conn(db)
    path = tmp_path / "notes.txt"
    path.write_text("Some notes long enough to be stored as the text of a document. " * 3)
    f.attachment(conn, filename="notes.txt", text=None, file_path=str(path), mime_type="text/plain")
    conn.close()

    stats = run_phase1(str(db))

    assert "figures" not in stats


def _heavy_hour(db, tmp_path) -> None:
    """Twelve editions of a 200-row, 9-month workbook in one hour: more than a day of real
    report mail (no hour of the reference replica's last 30 days brought more than 8)."""
    conn = _conn(db)
    for i, month in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 9, 9]):
        path = tmp_path / f"volumes-{i}.xlsx"
        _workbook(path, month, rows=200)
        email_id = f.email(
            conn, subject=f"Volumes {i}", date_received=f"2026-{month:02d}-28T0{i % 10}:00:00Z"
        )
        f.attachment(
            conn,
            email_id=email_id,
            filename="Volumes 2026-09.xlsx",
            text=None,
            file_path=str(path),
            mime_type=XLSX,
            sha256=f"sha-{i}",
        )
    conn.close()


def test_a_heavy_hour_of_editions_is_read_whole(db, tmp_path):
    """Its cost is measured, not asserted here: CI's coverage runs it three times slower. On a
    Mac the hook read twelve editions of five such sheets in 0.76 s (README, "Figures")."""
    _heavy_hour(db, tmp_path)

    stats = run_phase1(str(db))

    assert (stats["figures"]["parsed"], stats["figures"]["deferred"]) == (12, 0)
    assert stats["figures"]["figures"] > 12 * 200


def test_past_its_time_box_the_hook_leaves_the_rest_to_the_backfill(db, tmp_path, monkeypatch):
    _heavy_hour(db, tmp_path)
    monkeypatch.setattr(figures, "INGEST_DEADLINE_S", 0.0)

    stats = run_phase1(str(db))
    conn = _conn(db)
    later = figures.parse_pending(conn)
    conn.close()

    assert (stats["figures"]["parsed"], stats["figures"]["deferred"]) == (0, 12)
    assert later["parsed"] == 12


def test_the_hook_is_bounded_by_its_own_time_box(monkeypatch):
    assert figures.INGEST_DEADLINE_S <= attachment_pipeline_deadline()


def attachment_pipeline_deadline() -> float:
    from src.pipeline.sync import PHASE1_SYNC_DEADLINE_S

    return PHASE1_SYNC_DEADLINE_S

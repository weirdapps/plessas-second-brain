"""`brain maint figures-backfill`: every edition held, read into the registry, resumably.

A dry run by default, which only reads (so it runs on a replica, where --apply refuses);
--estimate parses a sample to project the figures and the time; --apply records the editions
and reads them, committing each, so a run stopped part way resumes where it stopped; --limit
bounds one run; --reset starts again after a parser change.
"""

import re
from datetime import datetime

import openpyxl
import pytest

from src.extract.attachment_extractors import extract_text_from_file
from src.store.schema import create_database, get_connection
from tests import factories as f
from tests.brain_cli import brain

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    for month in (7, 8, 9, 10):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Line", *[datetime(2026, m, 28) for m in range(1, month + 1)]])
        ws.append(["Calls answered", *range(100, 100 + month)])
        book = tmp_path / f"calls-{month}.xlsx"
        wb.save(book)
        email_id = f.email(conn, subject="Calls", date_received=f"2026-{month:02d}-28T08:00:00Z")
        f.attachment(
            conn,
            email_id=email_id,
            filename=f"Calls 2026-{month:02d}.xlsx",
            text=extract_text_from_file(str(book), XLSX)["text"],
            sha256=f"sha-{month}",
            content_columns={"extraction_method": "openpyxl"},
        )
    conn.close()
    return path


def _count(db, table: str) -> int:
    conn = get_connection(str(db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_the_dry_run_counts_and_writes_nothing(db, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill")

    assert code == 0
    assert "DRY RUN" in out and "1, 4 editions" in out
    assert _count(db, "figure_editions") == 0


def test_the_estimate_projects_figures_from_a_sample(db, monkeypatch, capsys):
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--estimate"
    )

    assert code == 0 and "~34 figures" in out


def test_apply_reads_every_edition_and_a_second_run_finds_nothing_to_do(db, monkeypatch, capsys):
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--apply"
    )

    assert code == 0 and re.search(r"editions read +: 4,", out)
    assert _count(db, "figure_values") == 10

    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--apply"
    )
    assert code == 0 and re.search(r"editions read +: 0,", out)
    assert _count(db, "figure_values") == 10


def test_limit_bounds_a_run_and_the_next_resumes(db, monkeypatch, capsys):
    args = ("--db", str(db), "maint", "figures-backfill", "--apply", "--limit", "1")
    _, out, _ = brain(monkeypatch, capsys, *args)
    assert re.search(r"still waiting +: 3 ", out)

    brain(monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--apply")
    assert _count(db, "figure_values") == 10


def test_reset_needs_apply_and_reads_everything_again(db, monkeypatch, capsys):
    code, _, _ = brain(monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--reset")
    assert code == 2

    brain(monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--apply")
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--apply", "--reset"
    )
    assert code == 0 and re.search(r"editions read +: 4,", out)
    assert _count(db, "figure_values") == 10


def test_apply_refuses_on_a_replica_and_the_dry_run_does_not(db, monkeypatch, capsys):
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    dry, _, _ = brain(monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill")
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(db), "maint", "figures-backfill", "--apply"
    )

    assert dry == 0
    assert code == 3 and "replica" in out

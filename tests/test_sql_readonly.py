"""Tests for the read-only SQL layer behind the sql_query and sql_schema tools."""

import sqlite3
from pathlib import Path

import pytest

from src.store import sql_readonly


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A real store built by create_database, with three emails to read."""
    from src.store.schema import create_database

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.executescript(
        """
        INSERT INTO emails (id, message_id, date_received, subject, summary, sender_name,
                            sender_address, mailbox_name, content) VALUES
            (1, 1, '2026-03-01T10:00:00', 'Budget plan', 'first', 'Alice', 'a@example.com',
             'Inbox', 'short body'),
            (2, 2, '2026-03-02T10:00:00', 'Προϋπολογισμός 2027', 'second', 'Bob',
             'b@example.com', 'Inbox', 'x'),
            (3, 3, '2026-03-03T10:00:00', 'Third', 'third', 'Carol', 'c@example.com',
             'Inbox', 'y');
        """
    )
    conn.commit()
    conn.close()
    return path


def _email_count(db: Path) -> int:
    check = sqlite3.connect(db)
    try:
        return check.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    finally:
        check.close()


def test_select_returns_columns_and_rows(db):
    out = sql_readonly.run_query("SELECT id, subject FROM emails ORDER BY id", db_path=db)
    assert out["columns"] == ["id", "subject"]
    assert out["rows"][0] == [1, "Budget plan"]
    assert out["row_count"] == 3
    assert out["truncated"] is False


def test_row_cap_sets_truncated(db):
    out = sql_readonly.run_query("SELECT id FROM emails ORDER BY id", limit=2, db_path=db)
    assert out["rows"] == [[1], [2]]
    assert out["truncated"] is True


def test_limit_is_clamped_to_one_and_two_hundred(db):
    low = sql_readonly.run_query("SELECT id FROM emails ORDER BY id", limit=-5, db_path=db)
    assert low["row_count"] == 1
    high = sql_readonly.run_query("SELECT id FROM emails", limit=10_000, db_path=db)
    assert high["row_count"] == 3


def test_cell_cut(db):
    out = sql_readonly.run_query("SELECT printf('%.5000c', 'x') AS big", db_path=db)
    cell = out["rows"][0][0]
    assert cell.startswith("x" * sql_readonly.CELL_CHARS)
    assert "cut, 5000 chars" in cell


def test_total_size_cap(db, monkeypatch):
    monkeypatch.setattr(sql_readonly, "TOTAL_CHARS", 10)
    out = sql_readonly.run_query("SELECT subject FROM emails ORDER BY id", db_path=db)
    assert out["row_count"] == 1  # the first row always fits
    assert out["truncated"] is True


def test_blob_cells_are_described_not_dumped(db):
    out = sql_readonly.run_query("SELECT zeroblob(16) AS b", db_path=db)
    assert out["rows"] == [["<16 bytes>"]]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO emails (id, message_id, subject, mailbox_name) VALUES (9, 9, 'x', 'Inbox')",
        "UPDATE emails SET subject = 'x'",
        "DELETE FROM emails",
        "CREATE TABLE t (x)",
        "DROP TABLE emails",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA journal_mode = DELETE",
        "PRAGMA query_only = OFF",
        "SELECT 1; DELETE FROM emails",
        "VACUUM",
    ],
)
def test_anything_but_reading_is_refused(db, sql):
    out = sql_readonly.run_query(sql, db_path=db)
    assert "error" in out
    assert _email_count(db) == 3


def test_fts_match_works_through_the_authorizer(db):
    out = sql_readonly.run_query(
        "SELECT rowid FROM emails_fts WHERE emails_fts MATCH 'budget'", db_path=db
    )
    assert "error" not in out, out
    assert out["rows"] == [[1]]


def test_sb_fold_matches_greek_without_accents(db):
    out = sql_readonly.run_query(
        "SELECT id FROM emails WHERE sb_fold(subject) LIKE '%' || sb_fold('ΠΡΟΥΠΟΛΟΓΙΣΜΟΣ') || '%'",
        db_path=db,
    )
    assert out["rows"] == [[2]]


def test_runaway_query_stops_at_the_budget(db, monkeypatch):
    monkeypatch.setattr(sql_readonly, "BUDGET_SECONDS", 0.0)
    out = sql_readonly.run_query(
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT count(*) FROM n",
        db_path=db,
    )
    assert "budget" in out["error"]


def test_missing_database_is_an_error_not_a_crash(tmp_path):
    out = sql_readonly.run_query("SELECT 1", db_path=tmp_path / "absent.db")
    assert "not found" in out["error"]


def _capture_uris(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        seen.append(database)
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sql_readonly.sqlite3, "connect", spy)
    return seen


def test_replica_opens_immutable(db, tmp_path, monkeypatch):
    stamp = tmp_path / "db-pull.stamp"
    stamp.write_text("pulled")
    monkeypatch.setattr(sql_readonly, "REPLICA_STAMP", stamp)
    seen = _capture_uris(monkeypatch)
    sql_readonly.connect_read_only(db).close()
    assert seen[0].endswith("?mode=ro&immutable=1")


def test_producer_opens_plain_read_only(db, tmp_path, monkeypatch):
    monkeypatch.setattr(sql_readonly, "REPLICA_STAMP", tmp_path / "absent.stamp")
    seen = _capture_uris(monkeypatch)
    sql_readonly.connect_read_only(db).close()
    assert seen[0].endswith("?mode=ro")


def test_describe_lists_tables_without_fts_shadows(db):
    out = sql_readonly.describe(db_path=db)
    names = {t["table"] for t in out["tables"]}
    assert {"emails", "emails_fts"} <= names
    assert not any(
        n.endswith(("_fts_data", "_fts_idx", "_fts_docsize", "_fts_config")) for n in names
    )
    emails = next(t for t in out["tables"] if t["table"] == "emails")
    assert emails["rows"] == 3


def test_describe_lists_full_text_tables_without_counting_them(db):
    tables = {t["table"]: t for t in sql_readonly.describe(db_path=db)["tables"]}
    assert tables["emails_fts"]["rows"] is None
    assert tables["emails_fts"]["virtual"] is True
    assert tables["emails"]["rows"] == 3
    assert tables["emails"]["virtual"] is False


def test_describe_one_table(db):
    out = sql_readonly.describe("emails", db_path=db)
    columns = {c["name"] for c in out["columns"]}
    assert {"id", "subject", "content"} <= columns
    assert isinstance(out["indexes"], list)


def test_describe_unknown_table(db):
    assert "no table" in sql_readonly.describe("nope", db_path=db)["error"]

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


def test_total_size_cap_drops_whole_rows_and_leaves_small_ones_untouched(db, monkeypatch):
    monkeypatch.setattr(sql_readonly, "TOTAL_CHARS", 25)
    out = sql_readonly.run_query("SELECT subject FROM emails ORDER BY id", db_path=db)
    assert out["rows"] == [["Budget plan"]]  # 11 chars fit; 11 + 19 would not
    assert out["truncated"] is True


def test_a_first_row_wider_than_the_answer_cap_is_cut_to_fit(db):
    wide = ", ".join(f"printf('%.5000c', 'x') AS c{i}" for i in range(sql_readonly.MAX_COLUMNS))
    out = sql_readonly.run_query(f"SELECT {wide}", db_path=db)
    row = out["rows"][0]
    assert sum(len(cell) for cell in row) <= sql_readonly.TOTAL_CHARS
    assert all(cell.endswith("… [cut, 5000 chars]") for cell in row)
    assert out["truncated"] is True


def test_rows_past_the_total_cap_are_never_read(db, monkeypatch):
    # Row 4 raises if SQLite ever evaluates it: json() of text that is not JSON.
    # Row 2 overflows the cap, and the sqlite3 cursor steps one row ahead of the
    # row it hands over, so streaming evaluates rows 1 to 3 and never row 4. A
    # reader that fetches a batch before applying the cap hits the error.
    monkeypatch.setattr(sql_readonly, "TOTAL_CHARS", 15)
    out = sql_readonly.run_query(
        "WITH t(x) AS (VALUES (1), (2), (3), (4), (5)) "
        "SELECT x, CASE WHEN x < 4 THEN printf('%.10c', 'x') ELSE json('no ' || x) END FROM t",
        db_path=db,
    )
    assert "error" not in out, out
    assert out["rows"] == [[1, "xxxxxxxxxx"]]
    assert out["truncated"] is True


def test_blob_cells_are_described_not_dumped(db):
    out = sql_readonly.run_query("SELECT zeroblob(16) AS b", db_path=db)
    assert out["rows"] == [["<16 bytes>"]]


def test_value_over_the_size_limit_is_an_error_not_a_memory_spike(db, monkeypatch):
    # The limit also applies to the schema SQLite reads on the first statement, so
    # it must stay above the longest CREATE text (about 1.8 KB). The value is built
    # the way a careless query builds one, by joining rows: 3 x 4,000 + 2 bytes.
    # printf() alone cannot show this; past the limit it returns NULL, not an error.
    monkeypatch.setattr(sql_readonly, "MAX_VALUE_BYTES", 10_000)
    out = sql_readonly.run_query(
        "SELECT group_concat(printf('%.4000c', 'x')) FROM emails", db_path=db
    )
    assert "too big" in out["error"]
    assert "substr(" in out["error"]
    normal = sql_readonly.run_query("SELECT id, subject FROM emails ORDER BY id", db_path=db)
    assert normal["row_count"] == 3


def test_every_table_fits_under_the_column_limit(db):
    # Below the widest table SQLite cannot read the schema and every query fails,
    # so this pins schema growth to the limit. table_xinfo counts generated columns.
    check = sqlite3.connect(db)
    try:
        names = [
            r[0]
            for r in check.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        ]
        widest = max(
            check.execute("SELECT count(*) FROM pragma_table_xinfo(?)", (name,)).fetchone()[0]
            for name in names
        )
    finally:
        check.close()
    assert widest < sql_readonly.MAX_COLUMNS


def test_a_result_wider_than_the_column_limit_says_to_name_columns(db):
    wide = ", ".join(f"{i} AS c{i}" for i in range(sql_readonly.MAX_COLUMNS + 1))
    out = sql_readonly.run_query(f"SELECT {wide}", db_path=db)
    assert "name only the columns" in out["error"]


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


def test_refusal_text_never_doubles_the_period(db):
    # SQLite's own message for this one already ends in a period.
    out = sql_readonly.run_query("SELECT 1; SELECT 2", db_path=db)
    assert out["error"].startswith("refused: ")
    assert ".." not in out["error"]


def test_pragma_names_match_in_any_case(db):
    out = sql_readonly.run_query("PRAGMA TABLE_INFO(emails)", db_path=db)
    assert "error" not in out, out
    assert "subject" in {row[1] for row in out["rows"]}


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


def test_a_file_that_is_not_a_database_is_an_error_not_a_crash(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database " * 100)
    assert "not a database" in sql_readonly.run_query("SELECT 1", db_path=bad)["error"]
    assert "not a database" in sql_readonly.describe(db_path=bad)["error"]


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


def test_connection_refuses_writes_without_the_authorizer(db):
    # describe() runs on this connection with no authorizer: mode=ro and
    # query_only are all that stand between it and a write.
    conn = sql_readonly.connect_read_only(db)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM emails")
    finally:
        conn.close()
    assert _email_count(db) == 3


def test_sorts_get_a_16_mb_cache_and_spill_to_files(db):
    conn = sql_readonly.connect_read_only(db)
    try:
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -16384
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 1  # FILE
    finally:
        conn.close()


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

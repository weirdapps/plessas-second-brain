"""v32: covering indexes keep the sweep and the health check from reading the stored text.

attachment_content keeps extracted_text ahead of the columns those queries need (status, method,
error, extracted_at, summary, llm_extracted_at). SQLite reaches a column behind a long text by
reading the text's overflow pages, so each such query read all of the text: 3 GB on the producer,
minutes per query, and the sweep runs inside the hourly sync's time budget. An index that holds
exactly what a query needs answers it without touching the table.
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store import file_sweep
from src.store.schema import (
    create_database,
    get_schema_version,
    migrate_add_attachment_content_indexes,
)

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"
INDEXES = {
    "idx_attachment_content_sweep",
    "idx_attachment_content_llm_extracted",
    "idx_attachment_content_summary_length",
}


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader, f"could not load {HEALTH_CHECK_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _indexes(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA index_list(attachment_content)")}


def _plan(conn, sql: str) -> str:
    return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))


def _attachment(conn, dirname: str, name: str, **content) -> int:
    """One attachments row, and an attachment_content row unless content is None."""
    cur = conn.execute(
        "INSERT INTO attachments (message_id, filename, mime_type, file_size, file_path,"
        " exported_at) VALUES (?, ?, 'application/pdf', 1, ?, '2026-10-01')",
        (dirname, name, f"/data/attachments/{dirname}/{name}"),
    )
    att_id = cur.lastrowid
    if content:
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
            " extraction_status, extraction_error, extracted_at, summary, llm_status,"
            " llm_extracted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                att_id,
                content.get("text", "some text"),
                content.get("method", "pymupdf"),
                content.get("status", "extracted"),
                content.get("error"),
                content.get("extracted_at", "2026-10-01T00:00:00"),
                content.get("summary"),
                content.get("llm_status", "pending"),
                content.get("llm_extracted_at"),
            ),
        )
    conn.commit()
    return att_id


# --- the migration -------------------------------------------------------------------------


def test_a_fresh_store_has_the_indexes(db):
    assert CURRENT_SCHEMA_VERSION >= 32
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
    assert INDEXES <= _indexes(db)


def test_the_migration_is_idempotent_and_leaves_the_busy_timeout_alone(db):
    db.execute("PRAGMA busy_timeout = 1234")
    migrate_add_attachment_content_indexes(db)
    migrate_add_attachment_content_indexes(db)
    assert INDEXES <= _indexes(db)
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 1234


def test_it_upgrades_a_store_that_predates_it(db):
    for name in INDEXES:
        db.execute(f"DROP INDEX {name}")
    db.commit()
    assert not INDEXES & _indexes(db)

    migrate_add_attachment_content_indexes(db)

    assert INDEXES <= _indexes(db)


def test_it_does_nothing_without_the_table():
    conn = sqlite3.connect(":memory:")
    migrate_add_attachment_content_indexes(conn)  # no attachment_content: nothing to index
    conn.close()


# --- the sweep ------------------------------------------------------------------------------


def test_the_sweep_query_joins_through_the_sweep_index(db):
    """Not the UNIQUE index on attachment_id: that one fetches every row from the table.

    The plan text says USING INDEX rather than COVERING INDEX because the query also asks for
    length(extracted_text); SQLite reads that from the index too (the indexed-expression
    optimization), which test_the_queries_never_read_the_stored_text proves.
    """
    plan = _plan(db, file_sweep.registered_sql(db))
    assert "idx_attachment_content_sweep (attachment_id=?)" in plan, plan


def test_a_store_without_the_index_still_sweeps(db):
    _attachment(db, "d1", "plain.pdf", text="hello")
    for name in INDEXES:
        db.execute(f"DROP INDEX {name}")
    assert "INDEXED BY" not in file_sweep.registered_sql(db)
    assert file_sweep._registered(db)[("d1", "plain.pdf")][1] == file_sweep.DELETABLE


def _poison_the_stored_text(path: Path) -> int:
    """Overwrite every overflow page of attachment_content, so reading a long text fails."""
    conn = sqlite3.connect(str(path))
    try:
        pages = [
            r[0]
            for r in conn.execute(
                "SELECT pageno FROM dbstat WHERE name = 'attachment_content'"
                " AND pagetype = 'overflow'"
            )
        ]
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no dbstat table")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with open(path, "r+b") as fh:
        for pageno in pages:
            fh.seek((pageno - 1) * page_size)
            fh.write(b"\xff" * page_size)
    return len(pages)


def test_the_queries_never_read_the_stored_text(tmp_path, hc):
    """The property the indexes exist for, proved on a store whose stored texts cannot be read.

    Each text is long enough to spill into overflow pages, and those pages are then destroyed. A
    query that reaches a column behind extracted_text through the table follows the chain and
    fails; one answered from an index never goes there.
    """
    path = tmp_path / "poisoned.db"
    conn = create_database(str(path))
    for i in range(20):
        _attachment(
            conn,
            f"d{i}",
            f"f{i}.pdf",
            text="x" * 50_000,
            summary="a summary",
            llm_status="extracted",
            llm_extracted_at="2026-10-01T00:00:00",
        )
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    assert _poison_the_stored_text(path) > 0

    conn = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.DatabaseError):  # the control: through the table it does fail
        conn.execute("SELECT extraction_method FROM attachment_content NOT INDEXED").fetchall()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("SELECT summary FROM attachment_content NOT INDEXED").fetchall()

    assert len(conn.execute(file_sweep.registered_sql(conn)).fetchall()) == 20
    assert conn.execute(hc.LATEST_LLM_EXTRACTION_SQL).fetchone()[0] == "2026-10-01T00:00:00"
    assert len(conn.execute(hc.SUMMARISED_ATTACHMENT_IDS_SQL).fetchall()) == 20
    conn.close()


def test_the_index_changes_no_sweep_state(db):
    _attachment(db, "d1", "plain.pdf", text="hello")
    _attachment(db, "d2", "sheet.xlsx", method="openpyxl")  # read in part until reextract
    _attachment(db, "d3", "long.pdf", text="x" * 100_000)  # cut at the text ceiling
    _attachment(db, "d4", "gone.pdf", status="failed", error="File not found: /x/gone.pdf")
    _attachment(db, "d5", "scan.png", status="skipped", error="OCR returned insufficient text")
    _attachment(db, "d6", "shot.pdf", method="vision")  # a described picture keeps its image
    _attachment(db, "d8", "capped.pdf", error="text cut at 100,000 characters; unread, file kept")
    _attachment(db, "d10", "pending.pdf")  # no content row at all

    with_index = file_sweep._registered(db)
    for name in INDEXES:
        db.execute(f"DROP INDEX {name}")
    without_index = file_sweep._registered(db)

    states = {key: state for key, (_ids, state) in with_index.items()}
    assert states[("d1", "plain.pdf")] == file_sweep.DELETABLE
    assert states[("d2", "sheet.xlsx")] == file_sweep.UNREAD
    assert states[("d3", "long.pdf")] == file_sweep.UNREAD
    assert states[("d4", "gone.pdf")] == file_sweep.UNREAD
    assert states[("d5", "scan.png")] == file_sweep.NOT_HELD
    assert states[("d6", "shot.pdf")] == file_sweep.NOT_HELD
    assert states[("d8", "capped.pdf")] == file_sweep.UNREAD
    assert states[("d10", "pending.pdf")] == file_sweep.PENDING_TEXT
    assert with_index == without_index


# --- the health check -----------------------------------------------------------------------


def test_the_latest_extraction_query_uses_its_index(db, hc):
    """Whether the plan says COVERING depends on the SQLite version; the property test proves it."""
    plan = _plan(db, hc.LATEST_LLM_EXTRACTION_SQL)
    assert "idx_attachment_content_llm_extracted" in plan, plan


def test_the_latest_extraction_is_the_newest_extracted_one(db, hc):
    _attachment(db, "d1", "a.pdf", llm_status="extracted", llm_extracted_at="2026-10-01T10:00:00")
    _attachment(db, "d2", "b.pdf", llm_status="extracted", llm_extracted_at="2026-10-03T10:00:00")
    _attachment(db, "d3", "c.pdf", llm_status="pending", llm_extracted_at="2026-10-09T10:00:00")
    assert db.execute(hc.LATEST_LLM_EXTRACTION_SQL).fetchone()[0] == "2026-10-03T10:00:00"


def test_the_summarised_attachment_query_uses_its_index(db, hc):
    """CI's older SQLite labels this plan USING INDEX where a current one says COVERING INDEX."""
    plan = _plan(db, hc.SUMMARISED_ATTACHMENT_IDS_SQL)
    assert "idx_attachment_content_summary_length" in plan, plan


def test_the_summarised_attachment_query_selects_what_the_old_condition_did(db, hc):
    for i, (summary, llm) in enumerate(
        [
            (None, "extracted"),
            ("", "extracted"),
            (" ", "extracted"),
            ("a summary", "extracted"),
            ("a summary", "pending"),
            ("é", "extracted"),
        ]
    ):
        _attachment(db, f"d{i}", f"f{i}.pdf", summary=summary, llm_status=llm)
    old = {
        r[0]
        for r in db.execute(
            "SELECT ac.id FROM attachment_content ac JOIN attachments a"
            " ON a.id = ac.attachment_id WHERE ac.llm_status = 'extracted'"
            " AND ac.summary IS NOT NULL AND ac.summary != ''"
        )
    }
    new = {r[0] for r in db.execute(hc.SUMMARISED_ATTACHMENT_IDS_SQL)}
    assert new == old and len(new) == 3

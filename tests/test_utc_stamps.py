"""Attachment and sync stamps are UTC with an offset, and the local ones stored before convert.

attachment_content.extracted_at and llm_extracted_at, and sync_metadata's last_sync_date, were
written in the producer's local time with no offset while the store's other stamps are UTC, so a
lag worked out against them, or by SQLite's julianday(), came out up to three hours wrong and
could read negative (audit 2026-10-16). New stamps carry their offset; `brain maint utc-stamps`
converts the stored ones; until it has run, every reader compares instants, whatever the form.
"""

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from src.store import stamps
from src.store.schema import create_database
from tests.local_zone import local_zone


@pytest.fixture
def athens():
    with local_zone("Europe/Athens"):
        yield


@pytest.fixture
def db(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _content(conn, text="some text", extracted_at=None, llm_extracted_at=None) -> int:
    att = conn.execute(
        "INSERT INTO attachments (message_id, filename, mime_type, file_size, file_path,"
        " exported_at) VALUES (1, 'f.pdf', 'application/pdf', 1, '/x/f.pdf', '2026-10-01')"
    ).lastrowid
    ac = conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status, llm_extracted_at)"
        " VALUES (?, ?, 'pymupdf', 'extracted', ?, 'extracted', ?)",
        (att, text, extracted_at, llm_extracted_at),
    ).lastrowid
    conn.commit()
    return ac


# --- writing ---------------------------------------------------------------------------------


def test_a_new_stamp_is_utc_with_its_offset():
    stamp = stamps.utc_now()

    assert stamp.endswith("+00:00")
    assert abs((datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()) < 5


# --- reading both forms ----------------------------------------------------------------------


def test_a_stamp_with_no_offset_is_local_time(athens):
    summer = stamps.instant("2026-10-16T12:16:09.841728")
    winter = stamps.instant("2026-01-16T12:16:09")

    assert summer == datetime(2026, 10, 16, 9, 16, 9, 841728, tzinfo=UTC)
    assert winter == datetime(2026, 1, 16, 10, 16, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    "stamp",
    ["2026-10-16T09:16:09.841728+00:00", "2026-10-16T09:16:09.84172Z", "2026-10-16T12:16:09+03:00"],
)
def test_a_stamp_with_an_offset_keeps_it(stamp, athens):
    assert stamps.instant(stamp).replace(microsecond=0) == datetime(
        2026, 10, 16, 9, 16, 9, tzinfo=UTC
    )


@pytest.mark.parametrize("stamp", [None, "", "yesterday", "2026-13-45"])
def test_an_unreadable_stamp_is_none(stamp):
    assert stamps.instant(stamp) is None


def test_sqlite_reads_both_forms_as_the_same_instant(athens):
    conn = sqlite3.connect(":memory:")
    rows = ["2026-10-16T12:16:09", "2026-10-16T09:16:09+00:00", "2026-10-16T09:16:09Z", None]
    got = [
        conn.execute(f"SELECT {stamps.instant_sql('?1')}", (value,)).fetchone()[0] for value in rows
    ]

    assert got[0] == pytest.approx(got[1]) == pytest.approx(got[2])
    assert got[3] is None


# --- converting what was stored --------------------------------------------------------------


@pytest.mark.parametrize(
    ("naive", "utc"),
    [
        ("2026-10-16T12:16:09.841728", "2026-10-16T09:16:09.84172Z"),
        ("2026-01-16T12:16:09.000001", "2026-01-16T10:16:09.00000Z"),
        ("2026-10-16T12:16:09", "2026-10-16T09:16:09Z"),
    ],
)
def test_a_local_stamp_converts_to_utc_as_wide_as_it_was(naive, utc, athens):
    assert stamps.to_utc(naive) == utc
    if "." in naive:
        assert len(utc) == len(naive)  # so SQLite rewrites the row in place
    assert stamps.instant(utc) == stamps.instant(naive).replace(
        microsecond=stamps.instant(naive).microsecond // 10 * 10
    )


@pytest.mark.parametrize("stamp", ["2026-10-16T09:16:09+00:00", "2026-10-16T09:16:09.5Z", "", "x"])
def test_a_stamp_already_utc_or_unreadable_is_not_converted(stamp):
    assert stamps.to_utc(stamp) is None


def test_a_dry_run_counts_and_changes_nothing(db, athens):
    ac = _content(db, extracted_at="2026-10-16T12:00:00.000000", llm_extracted_at=None)
    _content(db, extracted_at="2026-10-16T09:00:00+00:00", llm_extracted_at="2026-10-16T09:01Z")
    db.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-10-16T12:18:02')"
    )
    db.commit()

    found = stamps.convert(db, apply=False)

    assert (found["extracted_at"], found["llm_extracted_at"], found["sync_metadata"]) == (1, 0, 1)
    assert found["updated"] == 0
    stored = db.execute("SELECT extracted_at FROM attachment_content WHERE id = ?", (ac,))
    assert stored.fetchone()[0] == "2026-10-16T12:00:00.000000"


def test_apply_converts_every_local_stamp_and_leaves_the_rest(db, athens):
    both = _content(
        db, extracted_at="2026-10-16T12:00:00.000000", llm_extracted_at="2026-10-16T12:05:00.000000"
    )
    utc = _content(db, extracted_at="2026-10-16T09:00:00+00:00", llm_extracted_at=None)
    db.executemany(
        "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
        [
            ("last_sync_date", "2026-10-16T12:18:02.240136"),
            ("reextract_partial_since", "2026-10-12T10:00:00.000000"),
            ("mail_export_ok_at", "2026-10-16T09:15:27.161462Z"),
            (
                "refetch_garbled:5",
                json.dumps(
                    {
                        "state": "fetched",
                        "attempts": 1,
                        "fetched_at": "2026-10-13T08:00:00.000000",
                        "last_attempt_at": "2026-10-13T08:00:00.000000",
                    }
                ),
            ),
        ],
    )
    db.commit()

    done = stamps.convert(db, apply=True)

    assert done["updated"] == 1 and done["sync_metadata"] == 2 and done["refetch_records"] == 1
    row = db.execute(
        "SELECT extracted_at, llm_extracted_at FROM attachment_content WHERE id = ?", (both,)
    ).fetchone()
    assert tuple(row) == ("2026-10-16T09:00:00.00000Z", "2026-10-16T09:05:00.00000Z")
    assert (
        db.execute("SELECT extracted_at FROM attachment_content WHERE id = ?", (utc,)).fetchone()[0]
        == "2026-10-16T09:00:00+00:00"
    )
    meta = dict(db.execute("SELECT key, value FROM sync_metadata"))
    assert meta["last_sync_date"] == "2026-10-16T09:18:02.240136+00:00"
    assert meta["reextract_partial_since"] == "2026-10-12T07:00:00+00:00"
    assert meta["mail_export_ok_at"] == "2026-10-16T09:15:27.161462Z"
    record = json.loads(meta["refetch_garbled:5"])
    assert record["fetched_at"] == record["last_attempt_at"] == "2026-10-13T05:00:00+00:00"
    assert record["state"] == "fetched"

    assert stamps.convert(db, apply=True)["updated"] == 0  # nothing left to convert


def test_a_stamp_written_since_the_scan_is_left_alone(db, athens, monkeypatch):
    """A writer that stamps a row between the scan and the update has the last word."""
    ac = _content(db, extracted_at="2026-10-16T12:00:00.000000")
    scan = stamps._local_rows

    def scan_then_write(conn, column):
        found = scan(conn, column)
        conn.execute(
            "UPDATE attachment_content SET extracted_at = '2026-10-16T09:30:00+00:00' WHERE id = ?",
            (ac,),
        )
        return found

    monkeypatch.setattr(stamps, "_local_rows", scan_then_write)

    done = stamps.convert(db, apply=True)

    assert (done["updated"], done["changed_meanwhile"]) == (0, 1)
    assert db.execute("SELECT extracted_at FROM attachment_content").fetchone()[0] == (
        "2026-10-16T09:30:00+00:00"
    )


def _wal_frames(conn) -> int:
    return conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[1]


def test_converting_rewrites_no_stored_text(tmp_path, athens):
    """The stamps sit behind extracted_text, 3 GB on the producer. Kept as wide as they were,
    each row is overwritten in place and only the pages whose bytes change are written; a
    stamp of another width would have the whole text written again."""
    path = tmp_path / "big.db"
    conn = create_database(str(path))
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    big = "x" * 2_000_000  # about 490 overflow pages of 4 KiB
    _content(
        conn,
        text=big,
        extracted_at="2026-10-16T12:00:00.000000",
        llm_extracted_at="2026-10-16T12:05:00.000000",
    )
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    stamps.convert(conn, apply=True)
    frames = _wal_frames(conn)

    assert frames < 20, frames
    # The control: the same row, a stamp one character wider, rewrites the text.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("UPDATE attachment_content SET extracted_at = extracted_at || '0'")
    conn.commit()
    assert _wal_frames(conn) > 400
    conn.close()


def test_the_conversion_does_not_reindex_the_text(db, athens):
    _content(db, text="ενοίκια καταστημάτων", extracted_at="2026-10-16T12:00:00.000000")
    calls = []
    db.set_trace_callback(calls.append)

    stamps.convert(db, apply=True)

    db.set_trace_callback(None)
    assert not any("attachment_content_fts" in sql for sql in calls)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM attachment_content_fts WHERE attachment_content_fts MATCH 'ενοικια'"
        ).fetchone()[0]
        == 1
    )


def test_the_command_refuses_a_replica(monkeypatch, capsys, tmp_path):
    from tests.brain_cli import brain

    monkeypatch.setenv("BRAIN_ROLE", "replica")
    code, _, err = brain(monkeypatch, capsys, "maint", "utc-stamps", "--apply")

    assert code == 2 and "replica" in err


def test_the_command_is_a_dry_run_unless_told(monkeypatch, capsys, tmp_path, athens):
    from tests.brain_cli import brain

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    _content(conn, extracted_at="2026-10-16T12:00:00.000000")
    conn.close()
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "utc-stamps")
    assert code == 0 and "DRY RUN" in out and "extracted_at" in out
    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "utc-stamps", "--apply")
    assert code == 0 and "updated" in out

    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT extracted_at FROM attachment_content").fetchone()[0].endswith("Z")
    conn.close()


# --- the writers -----------------------------------------------------------------------------


def _attachment_file(conn, tmp_path, text: str) -> int:
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-10-01', 'm')"
    )
    path = tmp_path / "note.txt"
    path.write_text(text)
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, 'note.txt', 'text/plain', 1, ?, '2026-10-01')",
        (str(path),),
    ).lastrowid
    conn.commit()
    return att


def test_phase_1_and_phase_2_stamp_in_utc(tmp_path, db):
    from src.extract import attachment_pipeline as ap

    att = _attachment_file(db, tmp_path, "A plan for the network, long enough to be read. " * 3)
    db_path = db.execute("PRAGMA database_list").fetchone()[2]

    ap.run_phase1(db_path)
    ac_id = db.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (att,)
    ).fetchone()[0]
    stats = {"processed": 0, "extracted": 0, "failed": 0}
    ap.store_phase2_result(db, stats, ac_id, 1, {"summary": "s", "language": "en"}, None, False)

    extracted, summarised = db.execute(
        "SELECT extracted_at, llm_extracted_at FROM attachment_content WHERE id = ?", (ac_id,)
    ).fetchone()
    assert stamps.has_offset(extracted) and stamps.has_offset(summarised)


def test_a_text_document_is_stamped_in_utc(db):
    from src.extract.attachment_pipeline import ingest_text_document

    ingest_text_document(
        db,
        source="sharepoint-page",
        key="/sites/x/page.aspx",
        filename="page.aspx",
        mime_type="text/html",
        text="Some page text",
        sha256="ab" * 32,
        method="sharepoint-page",
        status="extracted",
        error=None,
        subject="[SharePoint page] x",
        sender_name="SharePoint",
        date="2026-10-01T09:00:00Z",
    )

    (extracted,) = db.execute("SELECT extracted_at FROM attachment_content").fetchone()
    assert stamps.has_offset(extracted)


# --- the readers, while both forms are stored --------------------------------------------------


def test_the_oldest_summary_waiting_is_the_oldest_instant(db, athens):
    from src.store.attachment_backlog import attachment_backlog

    for stamp in ("2026-10-16T12:00:00.000000", "2026-10-16T09:30:00+00:00"):  # 09:00Z, 09:30Z
        ac = _content(db, extracted_at=stamp)
        db.execute("UPDATE attachment_content SET llm_status = 'pending' WHERE id = ?", (ac,))
    db.commit()

    _text, summary = attachment_backlog(db)

    assert (summary.waiting, summary.oldest) == (2, "2026-10-16T09:00:00.000Z")


def _sweep_flag(conn, sql: str, ac_id: int) -> int:
    return conn.execute(
        f"SELECT {sql} FROM attachments a JOIN attachment_content ac ON ac.attachment_id = a.id"
        " WHERE ac.id = ?",
        (ac_id,),
    ).fetchone()[0]


def test_a_file_fetched_after_its_reading_is_kept_whatever_the_forms(db, athens):
    from src.store.file_sweep import REFETCH_KEY, REFETCHED_UNREAD_SQL

    ac = _content(db, extracted_at="2026-10-16T12:00:00.000000")  # read at 09:00Z
    att = db.execute("SELECT attachment_id FROM attachment_content WHERE id = ?", (ac,))
    db.execute(
        "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
        (f"{REFETCH_KEY}{att.fetchone()[0]}", json.dumps({"fetched_at": "2026-10-16T10:00Z"})),
    )
    db.commit()

    assert _sweep_flag(db, REFETCHED_UNREAD_SQL, ac) == 1  # as text, 10:00Z sorts before 12:00


def test_a_row_read_after_the_partial_mark_is_read_whatever_the_forms(db, athens):
    from src.store.file_sweep import PARTIAL_SINCE_KEY, PARTIAL_SQL

    ac = _content(db, extracted_at="2026-10-12T08:00:00+00:00")
    db.execute("UPDATE attachment_content SET extraction_method = 'openpyxl' WHERE id = ?", (ac,))
    db.execute(  # the mark, in local time: 07:00Z
        "INSERT INTO sync_metadata (key, value) VALUES (?, '2026-10-12T10:00:00.000000')",
        (PARTIAL_SINCE_KEY,),
    )
    db.commit()
    partial = f"COALESCE({PARTIAL_SQL}, 0)"

    assert _sweep_flag(db, partial, ac) == 0
    db.execute(
        "UPDATE attachment_content SET extracted_at = '2026-10-12T06:00:00Z' WHERE id = ?", (ac,)
    )
    assert _sweep_flag(db, partial, ac) == 1


def test_the_spend_check_counts_a_summary_in_its_utc_day(db, athens):
    import importlib.util
    from datetime import date
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health_check", Path(__file__).parent.parent / "scripts" / "health_check.py"
    )
    assert spec and spec.loader
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    # 01:30 in Athens on the 16th is 22:30 UTC on the 15th.
    _content(db, llm_extracted_at="2026-10-16T01:30:00.000000")
    _content(db, llm_extracted_at="2026-10-16T08:00:00+00:00")

    assert hc._model_work_on(db, date(2026, 10, 15))[1] == 1
    assert hc._model_work_on(db, date(2026, 10, 16))[1] == 1
    assert db.execute(hc.LATEST_LLM_EXTRACTION_SQL).fetchone()[0] == "2026-10-16T08:00:00.000Z"

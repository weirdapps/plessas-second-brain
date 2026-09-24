"""scripts/scrub_secrets.py removes credentials that reached brain.db before
redaction existed on the ingest path.

Redaction since #55 only applies to new data. The replica still carried 24
conversation turns with Anthropic, Google and Telegram keys, plus hits in email
bodies and attachment text, all dated before the fix, in every replica and every
snapshot taken since. A scrub that rewrites the row but leaves the old bytes in a
free page or an FTS segment is not a scrub, so the tests below read the database
FILE, not just the rows.
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from src.store.schema import create_database

SCRIPT = Path(__file__).parent.parent / "scripts" / "scrub_secrets.py"
# Shape fixtures, not credentials (same construction as tests/test_redact.py,
# trimmed to the exact 39 characters the google-key pattern consumes).
GOOGLE = "AIzaSy" + "A1b2C3d4E5" * 3 + "fgh"
ANTHROPIC = "sk-ant-api03-" + "a1B2c3D4e5" * 9


@pytest.fixture
def scrub():
    spec = importlib.util.spec_from_file_location("scrub_secrets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(tmp_path, scrub=None, monkeypatch=None) -> Path:
    path = tmp_path / "brain.db"
    if scrub is not None and monkeypatch is not None:
        # The script takes no --db: it works on the configured database.
        monkeypatch.setattr(scrub, "DEFAULT_DB", path)
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        ("m1", "2026-03-01T10:00:00", f"quarterly figures attached, key {GOOGLE} inside"),
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        ("m2", "2026-03-02T10:00:00", "nothing sensitive in this one"),
    )
    conn.execute(
        "INSERT INTO conversations (session_id, started_at, created_at) VALUES ('s1', ?, ?)",
        ("2026-03-01T10:00:00", "2026-03-01T10:00:00"),
    )
    conn.execute(
        "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker, content) "
        "VALUES (1, 0, ?, 'assistant', ?)",
        ("2026-03-01T10:00:00", f"export ANTHROPIC_API_KEY={ANTHROPIC}"),
    )
    conn.commit()
    conn.close()
    return path


def _file_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


def test_dry_run_reports_and_changes_nothing(scrub, tmp_path, capsys, monkeypatch):
    path = _db(tmp_path, scrub, monkeypatch)
    before = _file_bytes(path)

    rc = scrub.main([])

    assert rc == 1  # hits present
    out = capsys.readouterr().out
    assert "emails.content: 1 row" in out
    assert "conversation_turns.content: 1 row" in out
    assert GOOGLE not in out and ANTHROPIC not in out  # counts, never values
    assert _file_bytes(path) == before


def test_apply_redacts_every_hit_and_nothing_else(scrub, tmp_path, monkeypatch):
    path = _db(tmp_path, scrub, monkeypatch)

    assert scrub.main(["--apply"]) == 0

    conn = sqlite3.connect(path)
    m1, m2 = (r[0] for r in conn.execute("SELECT content FROM emails ORDER BY id"))
    turn = conn.execute("SELECT content FROM conversation_turns").fetchone()[0]
    conn.close()
    assert m1 == "quarterly figures attached, key [REDACTED:google-key] inside"
    assert m2 == "nothing sensitive in this one"
    assert turn == "export ANTHROPIC_API_KEY=[REDACTED:anthropic-key]"


def test_apply_leaves_no_copy_in_the_database_file(scrub, tmp_path, monkeypatch):
    """Not in a free page, not in an FTS segment, not in the WAL, and not in the
    lowercased form the full-text tokenizer stores."""
    path = _db(tmp_path, scrub, monkeypatch)
    assert GOOGLE.encode() in _file_bytes(path)

    scrub.main(["--apply"])

    data = _file_bytes(path)
    for secret in (GOOGLE, ANTHROPIC):
        assert secret.encode() not in data
        assert secret.lower().encode() not in data


def test_full_text_index_stays_consistent(scrub, tmp_path, monkeypatch):
    path = _db(tmp_path, scrub, monkeypatch)

    scrub.main(["--apply"])

    conn = sqlite3.connect(path)
    for fts in ("emails_fts", "conversation_turns_fts"):
        conn.execute(f"INSERT INTO {fts}({fts}) VALUES('integrity-check')")
    hits = conn.execute(
        "SELECT COUNT(*) FROM emails_fts WHERE emails_fts MATCH 'quarterly'"
    ).fetchone()[0]
    conn.close()
    assert hits == 1  # the redacted email is still findable by its other words


def test_a_second_apply_is_a_no_op(scrub, tmp_path, capsys, monkeypatch):
    _db(tmp_path, scrub, monkeypatch)
    scrub.main(["--apply"])
    capsys.readouterr()

    assert scrub.main(["--apply"]) == 0
    assert "0 rows redacted" in capsys.readouterr().out
    assert scrub.main([]) == 0  # the dry run now finds nothing


def test_a_missing_database_is_an_error(scrub, tmp_path, monkeypatch):
    monkeypatch.setattr(scrub, "DEFAULT_DB", tmp_path / "absent.db")
    assert scrub.main([]) == 2


def _traced(scrub, monkeypatch):
    """Record every statement the script's connections execute."""
    statements = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(scrub.sqlite3, "connect", connect)
    return statements


def test_apply_optimizes_every_fts_index_even_when_nothing_is_found(scrub, tmp_path, monkeypatch):
    """An interrupted run leaves the rows clean and the index dirty; a re-run
    must still finish the job. And a document deleted or re-ingested since it
    was indexed leaves its terms in segments whose content table shows no hit."""
    _db(tmp_path, scrub, monkeypatch)
    scrub.main(["--apply"])
    statements = _traced(scrub, monkeypatch)

    assert scrub.main(["--apply"]) == 0

    optimized = {s.split('"')[1] for s in statements if "VALUES('optimize')" in s}
    assert {
        "emails_fts",
        "conversation_turns_fts",
        "key_facts_fts",
        "teams_messages_fts",
    } <= optimized


def test_a_checkpoint_blocked_by_a_reader_is_reported_not_passed(
    scrub, tmp_path, monkeypatch, capsys
):
    """While a reader holds an old snapshot the WAL cannot be copied back, so
    the main file still holds the pre-scrub pages that the replica pull ships."""
    path = _db(tmp_path, scrub, monkeypatch)
    monkeypatch.setattr(scrub, "BUSY_TIMEOUT_MS", 200)
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM emails").fetchone()
    try:
        rc = scrub.main(["--apply"])
    finally:
        reader.close()

    assert rc == 1
    assert "checkpoint" in capsys.readouterr().err


def test_vacuum_requires_apply(scrub, tmp_path, monkeypatch):
    """A --vacuum on its own used to fall into the dry run and quietly do nothing."""
    _db(tmp_path, scrub, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        scrub.main(["--vacuum"])
    assert exc.value.code == 2


def test_vacuum_puts_its_temp_copy_beside_the_database(scrub, tmp_path, monkeypatch):
    """VACUUM builds a full temporary copy. Left to SQLite it goes to $TMPDIR,
    /var/tmp or /tmp, and on the producer /tmp is a tmpfs smaller than the
    database. SQLITE_TMPDIR set from inside the process is read too late (at
    import of sqlite3), so the directory is set on the connection."""
    path = _db(tmp_path, scrub, monkeypatch)
    statements = _traced(scrub, monkeypatch)

    assert scrub.main(["--apply", "--vacuum"]) == 0

    wanted = f"PRAGMA temp_store_directory = '{path.parent}'"
    assert any(s.strip() == wanted for s in statements), statements[:5]
    assert "SQLITE_TMPDIR" not in __import__("os").environ


def test_vacuum_removes_bytes_freed_before_the_scrub(scrub, tmp_path, monkeypatch):
    """secure_delete only zeroes what is freed while it is on. Copies freed by
    years of ordinary churn sit in freelist pages and slack, and only VACUUM
    rewrites them away."""
    path = _db(tmp_path, scrub, monkeypatch)
    conn = sqlite3.connect(path)  # ordinary churn, secure_delete off
    conn.execute("UPDATE emails SET summary = 'extracted' WHERE message_id = 'm1'")
    for i in range(200):  # later ingest: FTS merges free the old segment pages
        conn.execute(
            "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
            (f"churn{i}", "2026-03-03T10:00:00", f"routine message number {i} " * 20),
        )
    conn.commit()
    conn.close()

    assert scrub.main(["--apply", "--vacuum"]) == 0

    data = _file_bytes(path)
    for secret in (GOOGLE, ANTHROPIC):
        assert secret.encode() not in data
        assert secret.lower().encode() not in data


def test_a_key_in_kept_html_is_found_and_redacted(scrub, tmp_path, capsys, monkeypatch):
    """email_html holds compressed HTML (schema v23), so neither a scan of text
    columns nor a grep of the file can see a key in it: the scrub decompresses."""
    from src.store.email_html import pack, unpack

    other = "sk-ant-api03-" + "Z9y8X7w6V5" * 9  # a second shape, found nowhere else
    path = _db(tmp_path, scrub, monkeypatch)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO email_html (email_id, html) VALUES (2, ?)",
        (pack(f"<html><body><p>{other}</p></body></html>"),),
    )
    conn.commit()
    conn.close()
    assert other.encode() not in _file_bytes(path)

    assert scrub.main([]) == 1
    assert "email_html.html: 1 row" in capsys.readouterr().out

    assert scrub.main(["--apply"]) == 0
    conn = sqlite3.connect(path)
    (blob,) = conn.execute("SELECT html FROM email_html WHERE email_id = 2").fetchone()
    conn.close()
    assert unpack(blob) == "<html><body><p>[REDACTED:anthropic-key]</p></body></html>"
    assert scrub.main([]) == 0


@pytest.mark.parametrize(
    "bad",
    [b"not zlib at all", __import__("zlib").compress(b"\xff\xfe not utf-8"), "a text value"],
)
def test_an_unreadable_kept_html_is_counted_not_fatal(scrub, tmp_path, capsys, monkeypatch, bad):
    """One damaged email_html row stopped the scrub of every table, with the exit
    code that means 'found credentials'. It is counted, the rest is scrubbed, and
    the run exits 2: it could not check everything."""
    path = _db(tmp_path, scrub, monkeypatch)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO email_html (email_id, html) VALUES (2, ?)", (bad,))
    conn.commit()
    conn.close()

    assert scrub.main([]) == 2
    out = capsys.readouterr()
    assert "emails.content: 1 row" in out.out
    assert "1 email_html row could not be read" in out.err

    assert scrub.main(["--apply"]) == 2
    conn = sqlite3.connect(path)
    (m1,) = conn.execute("SELECT content FROM emails WHERE message_id = 'm1'").fetchone()
    conn.close()
    assert "[REDACTED:google-key]" in m1

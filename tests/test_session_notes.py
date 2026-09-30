"""What a Claude session writes to a note is ingested as a text-only document.

The transcript carries a Write's whole file and an Edit's change. They are replayed per path,
in order, and only when the tool call succeeded; the last version is stored, and a newer
version replaces the document of an older one.
"""

import json
from pathlib import Path

import pytest

from src.export.session_notes import collect_session_notes, ingest_session_notes
from src.store.schema import create_database

LONG = "Notes on the migration plan, with enough words in them to count as content."


def _write(path: Path, records: list) -> Path:
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n")
    return path


def _use(i, name, session="s1", ts=None, **inp):
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": ts or f"2026-09-30T10:00:{i:02d}Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"{session}-t{i}", "name": name, "input": inp}],
        },
    }


def _result(i, session="s1", error=False):
    return {
        "type": "user",
        "sessionId": session,
        "timestamp": f"2026-09-30T10:00:{i:02d}Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"{session}-t{i}", "is_error": error}
            ],
        },
    }


def _notes(tmp_path, *records):
    return collect_session_notes(_write(tmp_path / "s.jsonl", list(records)))


def test_the_last_write_wins(tmp_path):
    notes = _notes(
        tmp_path,
        _use(1, "Write", file_path="/n/a.md", content="first"),
        _result(1),
        _use(2, "Write", file_path="/n/a.md", content="second"),
        _result(2),
    )
    assert notes["/n/a.md"].text == "second"


def test_an_edit_is_replayed_onto_the_written_text(tmp_path):
    notes = _notes(
        tmp_path,
        _use(1, "Write", file_path="/n/a.md", content="alpha beta beta"),
        _result(1),
        _use(2, "Edit", file_path="/n/a.md", old_string="beta", new_string="gamma"),
        _result(2),
    )
    assert notes["/n/a.md"].text == "alpha gamma beta"


def test_replace_all_replaces_every_occurrence(tmp_path):
    notes = _notes(
        tmp_path,
        _use(1, "Write", file_path="/n/a.md", content="beta beta"),
        _result(1),
        _use(2, "Edit", file_path="/n/a.md", old_string="beta", new_string="x", replace_all=True),
        _result(2),
    )
    assert notes["/n/a.md"].text == "x x"


def test_an_edit_that_does_not_match_keeps_the_last_good_version(tmp_path):
    notes = _notes(
        tmp_path,
        _use(1, "Write", file_path="/n/a.md", content="alpha beta"),
        _result(1),
        _use(2, "Edit", file_path="/n/a.md", old_string="zeta", new_string="x"),
        _result(2),
        _use(3, "Edit", file_path="/n/a.md", old_string="beta", new_string="gamma"),
        _result(3),
    )
    assert notes["/n/a.md"].text == "alpha beta"


def test_multiedit_applies_all_its_edits_or_none(tmp_path):
    notes = _notes(
        tmp_path,
        _use(1, "Write", file_path="/n/a.md", content="one two"),
        _result(1),
        _use(
            2,
            "MultiEdit",
            file_path="/n/a.md",
            edits=[
                {"old_string": "one", "new_string": "1"},
                {"old_string": "missing", "new_string": "x"},
            ],
        ),
        _result(2),
        _use(3, "Write", file_path="/n/b.md", content="one two"),
        _result(3),
        _use(
            4,
            "MultiEdit",
            file_path="/n/b.md",
            edits=[
                {"old_string": "one", "new_string": "1"},
                {"old_string": "two", "new_string": "2"},
            ],
        ),
        _result(4),
    )
    assert notes["/n/a.md"].text == "one two"
    assert notes["/n/b.md"].text == "1 2"


def test_a_failed_tool_call_is_ignored(tmp_path):
    notes = _notes(
        tmp_path, _use(1, "Write", file_path="/n/a.md", content="draft"), _result(1, error=True)
    )
    assert notes == {}


def test_only_md_txt_and_csv_count(tmp_path):
    records = []
    for i, name in enumerate(("/n/a.py", "/n/b.csv", "/n/c.TXT", "/n/d.md"), 1):
        records += [_use(i, "Write", file_path=name, content="x"), _result(i)]
    assert set(_notes(tmp_path, *records)) == {"/n/b.csv", "/n/c.TXT", "/n/d.md"}


def test_malformed_records_are_skipped(tmp_path):
    notes = _notes(
        tmp_path,
        "not json at all",
        {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
        {"type": "user", "message": {"content": "plain text"}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        _use(1, "Write", file_path="/n/a.md", content="kept"),
        _result(1),
    )
    assert notes["/n/a.md"].text == "kept"


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _session(tmp_path, name, text, ts="2026-09-30T10:00:01Z", path="/n/plan.md"):
    return _write(
        tmp_path / f"{name}.jsonl",
        [_use(1, "Write", session=name, ts=ts, file_path=path, content=text), _result(1, name)],
    )


def _note_texts(conn, path="/n/plan.md"):
    return [
        r[0]
        for r in conn.execute(
            "SELECT ac.extracted_text FROM attachment_content ac"
            " JOIN attachments a ON a.id = ac.attachment_id WHERE a.file_path = ?",
            (f"text:session-note:{path}",),
        )
    ]


def test_a_note_becomes_a_text_document(conn, tmp_path):
    stats = ingest_session_notes(conn, [_session(tmp_path, "s1", LONG)])

    assert stats["stored"] == 1
    subject = conn.execute(
        "SELECT subject FROM emails WHERE sender_address = 'session-note@documents.local'"
    ).fetchone()[0]
    assert subject == "[Session note] /n/plan.md"
    assert _note_texts(conn) == [LONG]


def test_unchanged_content_is_left_alone(conn, tmp_path):
    f = _session(tmp_path, "s1", LONG)
    ingest_session_notes(conn, [f])

    assert ingest_session_notes(conn, [f])["unchanged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1


def test_a_newer_version_replaces_the_old_document(conn, tmp_path):
    ingest_session_notes(conn, [_session(tmp_path, "s1", LONG)])

    stats = ingest_session_notes(
        conn, [_session(tmp_path, "s2", LONG + " Revised.", ts="2026-09-30T11:00:00Z")]
    )

    assert stats["replaced"] == 1
    assert _note_texts(conn) == [LONG + " Revised."]
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1


def test_an_older_session_does_not_replace_a_newer_note(conn, tmp_path):
    ingest_session_notes(
        conn, [_session(tmp_path, "s2", LONG + " Newer.", ts="2026-09-30T11:00:00Z")]
    )

    stats = ingest_session_notes(conn, [_session(tmp_path, "s1", LONG)])

    assert stats["older"] == 1
    assert _note_texts(conn) == [LONG + " Newer."]


def test_a_document_two_paths_share_is_kept_when_one_changes(conn, tmp_path):
    ingest_session_notes(
        conn,
        [
            _session(tmp_path, "s1", LONG, path="/n/a.md"),
            _session(tmp_path, "s2", LONG, path="/n/b.md"),
        ],
    )
    ingest_session_notes(
        conn,
        [_session(tmp_path, "s3", LONG + " Changed.", ts="2026-09-30T12:00:00Z", path="/n/a.md")],
    )

    b_doc = conn.execute("SELECT message_id FROM session_notes WHERE path = '/n/b.md'").fetchone()[
        0
    ]
    assert (
        conn.execute("SELECT COUNT(*) FROM emails WHERE message_id = ?", (b_doc,)).fetchone()[0]
        == 1
    )


def test_the_scan_takes_only_transcripts_changed_since_the_last_one(conn, tmp_path, monkeypatch):
    import os
    import time

    from src.export import session_notes

    old = _session(tmp_path, "old", LONG)
    new = _session(tmp_path, "new", LONG + " New.")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    monkeypatch.setattr(session_notes, "scan_conversation_files", lambda: [new, old])
    session_notes.mark_scanned(conn, time.time() - 3600)

    assert session_notes.transcripts_to_scan(conn, all_files=False) == [new]
    assert session_notes.transcripts_to_scan(conn, all_files=True) == [new, old]


def test_a_document_another_source_holds_is_kept_when_the_note_changes(conn, tmp_path):
    """A note whose bytes equal a SharePoint document shares that document's identity. Its next
    version must not forget the document the SharePoint link still points at."""
    import hashlib

    from src.export.sharepoint_fetcher import record_link_in_db
    from src.extract.attachment_pipeline import ingest_text_document

    sha = hashlib.sha256(LONG.encode("utf-8")).hexdigest()
    shared = ingest_text_document(
        conn,
        source="sharepoint",
        key=sha,
        filename="plan.md",
        mime_type="text/markdown",
        text=LONG,
        sha256=sha,
        method="direct_read",
        status="extracted",
        error=None,
        subject="[SharePoint] host/plan.md",
        sender_name="SharePoint",
        date="2026-09-29T10:00:00",
    )
    record_link_in_db(
        conn,
        url="https://host/plan.md",
        message_id="AAMk-1",
        status="ok",
        document_message_id=shared["message_id"],
    )
    ingest_session_notes(conn, [_session(tmp_path, "s1", LONG)])

    ingest_session_notes(
        conn, [_session(tmp_path, "s2", LONG + " Revised.", ts="2026-09-30T11:00:00Z")]
    )

    kept = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE message_id = ?", (shared["message_id"],)
    ).fetchone()[0]
    assert kept == 1

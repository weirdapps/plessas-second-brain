"""What a Claude session writes to a note, ingested as a text-only document.

Conversation ingestion keeps only a "[Tool: Write <path>]" marker for a file a session wrote
(src/export/conversation_export.py). The content is in the transcript: a Write carries the whole
file, and an Edit or MultiEdit carries a change. They are replayed per path, in order, counting
a call only when its tool result succeeded (a write a hook refused never happened). The last
version of each note is stored through ingest_text_document and recorded in session_notes, so a
newer version replaces the document of an older one.

An edit whose old text is no longer there means the replay has lost track of the file: the note
keeps its last version known to be right and ignores that path's edits until the next Write.
Only .md, .txt and .csv count. Files written by scripts or through Bash are not covered: the
transcript does not carry their content.

Which transcripts to read is decided per file, by its size and modification time against those
recorded when it was last read (session_note_transcripts), never by a clock mark: transcripts
reach this host by rsync with the writing Mac's times, so a laptop that syncs late delivers files
that look older than the last scan. Subagent and workflow transcripts, under a session's own
directory, are read too. One transcript's failure is counted and the others go on; it is read
again next run.
"""

import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.config import CLAUDE_CODE_PROJECTS_DIR
from src.extract.attachment_extractors import _apply_noise_filter
from src.extract.attachment_pipeline import _guess_mime_type, ingest_text_document
from src.store.forget import forget_documents

NOTE_SUFFIXES = (".md", ".txt", ".csv")
# On the first scan, transcripts older than this are left to the backfill (--all).
FIRST_SCAN_DAYS = 2


@dataclass
class Note:
    path: str
    text: str
    written_at: str
    session_id: str


def _records(jsonl_path: Path):
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except (OSError, UnicodeDecodeError):
        return


def _replay(text: str, edits) -> str | None:
    """Apply the edits in order, all or none; None when one no longer matches."""
    for edit in edits or ():
        if not isinstance(edit, dict):
            return None
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or not old or old not in text:
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def collect_session_notes(jsonl_path: Path) -> dict[str, Note]:
    """path -> the last version of each note this transcript wrote."""
    notes: dict[str, Note] = {}
    lost: set[str] = set()
    pending: dict[str, tuple[str, dict, str]] = {}
    session_id = jsonl_path.stem
    for record in _records(jsonl_path):
        # Every field is checked for its type: a transcript is third-party input to this code,
        # and one odd record must not stop the read.
        sid = record.get("sessionId")
        if isinstance(sid, str) and sid:
            session_id = sid
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        stamp = record.get("timestamp")
        timestamp = stamp if isinstance(stamp, str) else ""
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in (
                "Write",
                "Edit",
                "MultiEdit",
            ):
                use_id, tool_input = block.get("id"), block.get("input")
                if isinstance(use_id, str) and use_id and isinstance(tool_input, dict):
                    pending[use_id] = (block["name"], tool_input, timestamp)
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                use_id = block.get("tool_use_id")
                use = pending.pop(use_id, None) if isinstance(use_id, str) else None
                if use is None:
                    continue
                name, tool_input, timestamp = use
                path = tool_input.get("file_path")
                if not isinstance(path, str) or not path.lower().endswith(NOTE_SUFFIXES):
                    continue
                if name == "Write":
                    if isinstance(tool_input.get("content"), str):
                        notes[path] = Note(path, tool_input["content"], timestamp, session_id)
                        lost.discard(path)
                    continue
                if path not in notes or path in lost:
                    continue
                edits = tool_input.get("edits") if name == "MultiEdit" else [tool_input]
                text = _replay(notes[path].text, edits)
                if text is None:
                    lost.add(path)
                    continue
                notes[path] = Note(path, text, timestamp, session_id)
    return notes


def _held_elsewhere(conn: sqlite3.Connection, message_id: int, path: str) -> bool:
    """Whether anything but this note holds the document, so replacing the note must keep it.

    A note's identity is the hash of its bytes, the same identity SharePoint documents, imports
    and adopted attachments use. So the document may be another note path's, a SharePoint link's,
    or not a session note at all; only a document this note alone holds is forgotten.
    """
    if conn.execute(
        "SELECT 1 FROM session_notes WHERE message_id = ? AND path != ?", (message_id, path)
    ).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM sharepoint_links WHERE document_message_id = ?", (message_id,)
    ).fetchone():
        return True
    row = conn.execute(
        "SELECT file_path FROM attachments WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is None or not str(row[0]).startswith("text:session-note:")


def _store_note(conn: sqlite3.Connection, note: Note, stats: dict) -> None:
    sha = hashlib.sha256(note.text.encode("utf-8")).hexdigest()
    held = conn.execute(
        "SELECT message_id, written_at, sha256 FROM session_notes WHERE path = ?", (note.path,)
    ).fetchone()
    if held:
        if held[2] == sha:
            stats["unchanged"] += 1
            return
        if (held[1] or "") > (note.written_at or ""):
            stats["older"] += 1
            return
        if not _held_elsewhere(conn, held[0], note.path):
            forget_documents(conn, [held[0]])
        stats["replaced"] += 1
    outcome = ingest_text_document(
        conn,
        source="session-note",
        key=note.path,
        filename=Path(note.path).name,
        mime_type=_guess_mime_type(note.path),
        text=note.text,
        sha256=sha,
        method="session-note",
        status="skipped" if _apply_noise_filter(note.text) else "extracted",
        error=None,
        subject=f"[Session note] {note.path}",
        sender_name="Claude session",
        date=note.written_at or datetime.now().isoformat(),
    )
    conn.execute(
        """INSERT INTO session_notes (path, message_id, session_id, written_at, sha256)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET message_id = excluded.message_id,
             session_id = excluded.session_id, written_at = excluded.written_at,
             sha256 = excluded.sha256""",
        (note.path, outcome["message_id"], note.session_id, note.written_at, sha),
    )
    conn.commit()
    stats["stored"] += 1


def _mark_read(conn: sqlite3.Connection, path: Path, size: int, mtime_ns: int) -> None:
    conn.execute(
        """INSERT INTO session_note_transcripts (path, size, mtime_ns) VALUES (?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns""",
        (str(path), size, mtime_ns),
    )
    conn.commit()


def ingest_session_notes(conn: sqlite3.Connection, jsonl_files) -> dict:
    """Store the last version of every note the transcripts wrote. See the module docstring.

    A transcript is recorded as read with the size and time it had before the read, so one that
    grew meanwhile is read again. One that fails is counted in "errors", left unrecorded so the
    next run reads it again, and does not stop the others.
    """
    stats = dict.fromkeys(
        ("transcripts", "notes", "stored", "replaced", "unchanged", "older", "errors"), 0
    )
    for jsonl in jsonl_files:
        path = Path(jsonl)
        try:
            st = path.stat()
        except OSError:
            continue
        stats["transcripts"] += 1
        try:
            for note in collect_session_notes(path).values():
                stats["notes"] += 1
                _store_note(conn, note, stats)
        except Exception as e:
            conn.rollback()
            stats["errors"] += 1
            print(f"  session notes: {path}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        _mark_read(conn, path, st.st_size, st.st_mtime_ns)
    return stats


def _transcripts() -> list[Path]:
    """Every transcript: sessions at the top of each project, and the subagent and workflow
    transcripts under a session's own directory, which write plans, specs and reports too."""
    if not CLAUDE_CODE_PROJECTS_DIR.is_dir():
        return []
    return sorted(p for p in CLAUDE_CODE_PROJECTS_DIR.glob("*/**/*.jsonl") if p.is_file())


def transcripts_to_scan(conn: sqlite3.Connection, all_files: bool) -> list[Path]:
    """The transcripts new or changed since they were last read, or every one for the backfill.

    On the first scan (nothing recorded yet) transcripts older than FIRST_SCAN_DAYS are recorded
    as read without reading them: they are the backfill's (--all), and reading them all here
    would not fit the conversation sync's unit.
    """
    files = _transcripts()
    if all_files:
        return files
    seen = {
        path: (size, mtime_ns)
        for path, size, mtime_ns in conn.execute(
            "SELECT path, size, mtime_ns FROM session_note_transcripts"
        )
    }
    first = not seen
    cutoff = time.time() - FIRST_SCAN_DAYS * 86400
    todo = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        if first and st.st_mtime < cutoff:
            _mark_read(conn, f, st.st_size, st.st_mtime_ns)
            continue
        if seen.get(str(f)) != (st.st_size, st.st_mtime_ns):
            todo.append(f)
    return todo

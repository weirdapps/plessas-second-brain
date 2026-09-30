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
"""

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.export.conversation_export import scan_conversation_files
from src.extract.attachment_extractors import _apply_noise_filter
from src.extract.attachment_pipeline import _guess_mime_type, ingest_text_document
from src.store.forget import forget_documents

NOTE_SUFFIXES = (".md", ".txt", ".csv")
SCAN_MARK_KEY = "session_notes_scanned_to"
# How far back the first run without a mark reaches; the backfill is --all.
FIRST_SCAN_DAYS = 2
# A transcript still being written while the scan reads it is taken again next time.
SCAN_OVERLAP_S = 60


@dataclass
class Note:
    path: str
    text: str
    written_at: str
    session_id: str


def _records(jsonl_path: Path):
    try:
        with open(jsonl_path, encoding="utf-8") as f:
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
        session_id = record.get("sessionId") or session_id
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in (
                "Write",
                "Edit",
                "MultiEdit",
            ):
                if block.get("id") and isinstance(block.get("input"), dict):
                    pending[block["id"]] = (
                        block["name"],
                        block["input"],
                        record.get("timestamp") or "",
                    )
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
        shared = conn.execute(
            "SELECT COUNT(*) FROM session_notes WHERE message_id = ? AND path != ?",
            (held[0], note.path),
        ).fetchone()[0]
        if not shared:
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


def ingest_session_notes(conn: sqlite3.Connection, jsonl_files) -> dict:
    """Store the last version of every note the transcripts wrote. See the module docstring."""
    stats = dict.fromkeys(("notes", "stored", "replaced", "unchanged", "older"), 0)
    for jsonl in jsonl_files:
        for note in collect_session_notes(Path(jsonl)).values():
            stats["notes"] += 1
            _store_note(conn, note, stats)
    return stats


def transcripts_to_scan(conn: sqlite3.Connection, all_files: bool) -> list[Path]:
    """The transcripts changed since the last scan, or every one for the backfill."""
    files = scan_conversation_files()
    if all_files:
        return files
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (SCAN_MARK_KEY,)).fetchone()
    mark = float(row[0]) if row else time.time() - FIRST_SCAN_DAYS * 86400
    return [f for f in files if f.stat().st_mtime > mark]


def mark_scanned(conn: sqlite3.Connection, started: float) -> None:
    """Remember where the next scan starts: a little before this one began."""
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        (SCAN_MARK_KEY, str(started - SCAN_OVERLAP_S)),
    )
    conn.commit()

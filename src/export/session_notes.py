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

A ```findings block in a note also loads, line by line, as findings (src/store/findings.py):
the claims a research session verified, each naming the store item it rests on. Each line
fails closed: accepted only when its source is held and its quote or value is in that
source's text, else stored rejected with the reason. They are kept in step with the note's
last version, an unchanged one included, so a finding whose source arrived later is
accepted on the next read. Each records whether the owner's own session wrote the note or
an agent's (a teammate's or a subagent's), which ranks below.

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
from pathlib import Path

from src.config import CLAUDE_CODE_PROJECTS_DIR
from src.export.conversation_export import (
    _CONTENT_TYPES,
    _extract_text_content,
    is_agent_record,
    is_owner_record,
)
from src.extract.attachment_extractors import _apply_noise_filter
from src.extract.attachment_pipeline import _guess_mime_type, ingest_text_document
from src.store.findings import sync_note_findings
from src.store.forget import forget_documents
from src.store.stamps import utc_now

NOTE_SUFFIXES = (".md", ".txt", ".csv")
# On the first scan, transcripts older than this are left to the backfill (--all).
FIRST_SCAN_DAYS = 2


@dataclass
class Note:
    path: str
    text: str
    written_at: str
    session_id: str
    # Who ran the session whose tool call wrote it: 'owner' or 'agent' (author_kind).
    author_kind: str = "agent"


def _owner_turn(record: dict) -> bool:
    """Whether conversation export labels this record's turn speaker 'user', the owner
    typing (parse_conversation in src/export/conversation_export.py, the same functions),
    leaving aside the session-wide agent test, which author_kind applies."""
    if record.get("type") not in ("user", "assistant"):
        return False
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role", record.get("type")) != "user":
        return False
    try:
        text = _extract_text_content(message)
        return bool(text) and is_owner_record(record, text)
    except Exception:  # an odd record is no evidence of the owner
        return False


def _top_level(jsonl_path: Path) -> bool:
    """Whether the transcript is a session of its own at the top of a project
    (projects/<project>/<session>.jsonl), not one under a session's own directory, where
    its subagents' and workflows' transcripts are kept."""
    if "subagents" in jsonl_path.parts:
        return False
    try:
        inside = jsonl_path.resolve().relative_to(CLAUDE_CODE_PROJECTS_DIR.resolve())
    except (OSError, ValueError):
        return False
    return len(inside.parts) == 2


def author_kind(jsonl_path: Path, records) -> str:
    """'owner' when the transcript is a session of its own, no record of it names an agent
    (a teammate's name or team, a subagent's id), and it holds a turn the owner typed, as
    conversation export labels turns; 'agent' otherwise, and whenever that cannot be read.

    Only the transcript decides: never a note's text, its path or name, a field of its
    findings block, or the session's workspace.
    """
    if not _top_level(jsonl_path):
        return "agent"
    owner = False
    for record in records:
        if is_agent_record(record):
            return "agent"
        owner = owner or _owner_turn(record)
    return "owner" if owner else "agent"


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
    """path -> the last version of each note this transcript wrote.

    Only the turns are read, the records conversation export reads (_CONTENT_TYPES). Claude Code
    also writes each request it sends as an "api-request-blob" record, with no timestamp and
    every earlier tool call and result in it again: read as turns, it replayed each Write with no
    time, so 57 notes were filed under the hour they were ingested, in local time (audit
    2026-10-20), and each Edit onto a text it had already changed, which lost track of the file.
    A call whose record carries no time takes the last time before it.
    """
    notes: dict[str, Note] = {}
    lost: set[str] = set()
    pending: dict[str, tuple[str, dict, str]] = {}
    session_id = jsonl_path.stem
    last_stamp = ""
    records = list(_records(jsonl_path))
    for record in records:
        # Every field is checked for its type: a transcript is third-party input to this code,
        # and one odd record must not stop the read.
        sid = record.get("sessionId")
        if isinstance(sid, str) and sid:
            session_id = sid
        stamp = record.get("timestamp")
        if isinstance(stamp, str) and stamp:
            last_stamp = stamp
        if record.get("type") not in _CONTENT_TYPES:
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        timestamp = last_stamp
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
    if notes:
        author = author_kind(jsonl_path, records)
        for note in notes.values():
            note.author_kind = author
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


def _sync_findings(conn: sqlite3.Connection, note: Note, stats: dict) -> None:
    """The note's findings, in step with this version of it (not committed here)."""
    found = sync_note_findings(
        conn, note.path, note.session_id, note.written_at, note.text, author_kind=note.author_kind
    )
    stats["findings_accepted"] += found["accepted"]
    stats["findings_rejected"] += found["rejected"]


def _store_note(conn: sqlite3.Connection, note: Note, stats: dict) -> None:
    sha = hashlib.sha256(note.text.encode("utf-8")).hexdigest()
    held = conn.execute(
        "SELECT message_id, written_at, sha256 FROM session_notes WHERE path = ?", (note.path,)
    ).fetchone()
    if held:
        if held[2] == sha:
            stats["unchanged"] += 1
            if not held[1] and note.written_at:
                # Stored undated (collect_session_notes): it takes its time now, and so does
                # its document, unless another source's document holds these bytes.
                conn.execute(
                    "UPDATE session_notes SET written_at = ? WHERE path = ?",
                    (note.written_at, note.path),
                )
                conn.execute(
                    "UPDATE emails SET date_received = ? WHERE message_id = ?"
                    " AND sender_address = 'session-note@documents.local'",
                    (note.written_at, held[0]),
                )
                stats["dated"] += 1
            _sync_findings(conn, note, stats)
            conn.commit()
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
        # In UTC, as the transcript's own times are, when the transcript gives none.
        date=note.written_at or utc_now(),
    )
    conn.execute(
        """INSERT INTO session_notes (path, message_id, session_id, written_at, sha256)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET message_id = excluded.message_id,
             session_id = excluded.session_id, written_at = excluded.written_at,
             sha256 = excluded.sha256""",
        (note.path, outcome["message_id"], note.session_id, note.written_at, sha),
    )
    _sync_findings(conn, note, stats)
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
        (
            "transcripts",
            "notes",
            "stored",
            "replaced",
            "unchanged",
            "dated",
            "older",
            "errors",
            "findings_accepted",
            "findings_rejected",
        ),
        0,
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


def _session_of(jsonl_path: Path) -> str | None:
    """The session a transcript belongs to: its own name at the top of a project, the directory
    it sits under for a subagent's or a workflow's."""
    try:
        inside = jsonl_path.resolve().relative_to(CLAUDE_CODE_PROJECTS_DIR.resolve())
    except (OSError, ValueError):
        return None
    return Path(inside.parts[1]).stem if len(inside.parts) >= 2 else None


def _undated(conn: sqlite3.Connection) -> list[str]:
    """The session of each note held with no written_at, one per note."""
    return [
        sid or ""
        for (sid,) in conn.execute(
            "SELECT session_id FROM session_notes WHERE COALESCE(written_at, '') = ''"
        )
    ]


def date_undated_notes(conn: sqlite3.Connection, *, apply: bool) -> dict:
    """`brain maint session-note-dates`: read again the transcripts of the sessions whose notes
    are held undated, so each takes its time (_store_note) and its document with it, in UTC.

    Counts: undated (notes), sessions, transcripts (theirs, held on this host), dated, left
    (still undated: a transcript gone, or a note no transcript writes now). A second run finds
    what the first left. Reads no other transcript; `ingest-session-notes --all` reads them all.
    """
    sessions = _undated(conn)
    wanted = set(sessions)
    files = [f for f in _transcripts() if _session_of(f) in wanted]
    stats = {
        "undated": len(sessions),
        "sessions": len(wanted),
        "transcripts": len(files),
        "dated": 0,
        "left": len(sessions),
    }
    if apply and files:
        stats["dated"] = ingest_session_notes(conn, files)["dated"]
        stats["left"] = len(_undated(conn))
    return stats


def cmd_session_note_dates(args) -> int:
    from src.store.schema import get_connection, run_migrations

    conn = get_connection(str(args.db))
    try:
        run_migrations(conn)
        stats = date_undated_notes(conn, apply=args.apply)
    finally:
        conn.close()
    print(
        f"{stats['undated']:,} session note(s) held undated, from {stats['sessions']:,}"
        f" session(s); {stats['transcripts']:,} of their transcript(s) on this host"
    )
    if args.apply:
        print(f"dated {stats['dated']:,}; {stats['left']:,} still undated")
    else:
        print("dry run: nothing read or written; --apply reads the transcripts again")
    return 0


def add_maint_parser(maint_subparsers) -> None:
    """`brain maint session-note-dates`."""
    parser = maint_subparsers.add_parser(
        "session-note-dates",
        help="Date the session notes stored undated by their transcripts' own times, in UTC",
        description=date_undated_notes.__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Count only (default)")
    mode.add_argument("--apply", action="store_true", help="Read their transcripts again")
    parser.set_defaults(func=cmd_session_note_dates)

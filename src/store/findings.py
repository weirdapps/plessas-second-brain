"""Findings a research session files back, parsed from a ```findings block in a note it wrote.

Research teammates make most of the brain's calls, and what they verify (a figure
reconciled, a claim corrected) ended in hand-edited memory files or was lost. A note a
session writes is already ingested as a text-only document (src/export/session_notes.py);
a fenced block in it now also loads, line by line, into the `findings` table (schema v37):

    ```findings
    {"claim": "Share of loan applications made online, YTD", "value": "64.0%", "as_of": "2026-08",
     "source_ref": "attachment:1234", "supersedes": "finding:17"}
    ```

One JSON object a line (the example is wrapped here only for width). `claim` is required;
`value`, `as_of`, `source_ref` and `supersedes` are optional. `source_ref` names the store
item the claim rests on as a row's `kind:ref` (what `get_item` reads). `supersedes` names an
earlier finding (`finding:<id>`) or the claim it replaces.

A finding is derived, and is treated so everywhere:
- it is `sourced` only when `source_ref` names a primary item the store holds; otherwise
  it is kept, with `source_check` saying why not;
- a finding, a topic page, a session or a session note is never a source, so no finding
  rests on another;
- it ranks below primary sources: a topic page lists findings after what the items say,
  and nothing indexes or embeds them.

A line that does not parse is counted and never echoed: a note may quote anything.
"""

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

from src.store.greek import search_fold

# The fence that opens a block: ```findings (or ~~~findings) on a line of its own.
_BLOCK = re.compile(
    r"^(?P<fence>```|~~~)[ \t]*findings[ \t]*\n(?P<body>.*?)^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_FIELDS = ("claim", "value", "as_of", "source_ref", "supersedes")
# The longest a stored field is kept: a claim is a sentence, not a report.
_FIELD_CHARS = 1_000
_REF = re.compile(r"^(?P<kind>[a-z_]+):(?P<ref>\S+)$")

# The primary kinds a source_ref may name, and the table and column each is read from.
# The integer ones take an integer ref.
_PRIMARY = {
    "email": ("emails", "id", True),
    "news": ("emails", "id", True),
    "document": ("emails", "id", True),
    "attachment": ("attachments", "id", True),
    "teams": ("teams_threads", "id", True),
    "whatsapp": ("whatsapp_threads", "id", True),
    "calendar": ("calendar_events", "id", True),
    "image": ("inline_images", "sha256", False),
}
# Kinds that are themselves derived, and why each cannot source a finding.
_DERIVED = {
    "finding": "a finding cannot be the source of a finding",
    "topic_page": "a topic page is derived",
    "conversation": "a session is derived, not a primary source",
}
SESSION_NOTE_DERIVED = "a session note is derived"


def _text(value) -> str | None:
    """A field as stored: text, numbers and booleans as JSON writes them, None for null or
    empty."""
    if value is None:
        return None
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    if isinstance(value, str):
        value = value.strip()
        return value[:_FIELD_CHARS] or None
    return json.dumps(value, ensure_ascii=False)[:_FIELD_CHARS]


def parse_findings(text: str | None) -> tuple[list[dict], list[str]]:
    """The findings of every ```findings block in `text`, in order, and what failed.

    Each error names the block and line and why, never the line itself.
    """
    rows: list[dict] = []
    errors: list[str] = []
    for block_no, match in enumerate(_BLOCK.finditer(text or ""), start=1):
        for line_no, line in enumerate(match.group("body").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            where = f"block {block_no} line {line_no}"
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"{where}: not one JSON object")
                continue
            if not isinstance(item, dict):
                errors.append(f"{where}: not a JSON object")
                continue
            row = {field: _text(item.get(field)) for field in _FIELDS}
            if not row["claim"]:
                errors.append(f"{where}: no claim")
                continue
            rows.append(row)
    return rows, errors


def _dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """The rows as dicts, whatever row factory the connection has."""
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _emails_class(conn: sqlite3.Connection, email_id) -> str | None:
    row = conn.execute("SELECT source_class FROM emails WHERE id = ?", (email_id,)).fetchone()
    return row[0] if row else None


def check_source(conn: sqlite3.Connection, ref: str | None) -> tuple[bool, str]:
    """Whether `ref` names a primary item this store holds, and if not, why."""
    if not ref:
        return False, "no source_ref"
    match = _REF.match(ref.strip())
    if not match:
        return False, "source_ref is not kind:ref"
    kind, key = match.group("kind"), match.group("ref")
    if kind in _DERIVED:
        return False, _DERIVED[kind]
    if kind not in _PRIMARY:
        return False, f"unknown kind {kind!r}"
    table, column, integer = _PRIMARY[kind]
    if integer and not key.lstrip("-").isdigit():
        return False, "source_ref is not kind:ref with an integer ref"
    value: int | str = int(key) if integer else key
    try:
        row = conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)).fetchone()
    except sqlite3.OperationalError:
        return False, "not in the store"
    if not row:
        return False, "not in the store"
    if table == "emails" and _emails_class(conn, value) == "session_note":
        return False, SESSION_NOTE_DERIVED
    if table == "attachments":
        parent = conn.execute("SELECT email_id FROM attachments WHERE id = ?", (value,)).fetchone()
        if parent and _emails_class(conn, parent[0]) == "session_note":
            return False, SESSION_NOTE_DERIVED
    return True, "ok"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_note_findings(
    conn: sqlite3.Connection,
    path: str,
    session_id: str | None,
    written_at: str | None,
    text: str | None,
) -> dict:
    """Make the findings of note `path` those its text holds now. Does not commit.

    One row per position in the note: an unchanged finding keeps its id and loaded_at,
    so a topic page that cites it stays valid; a changed one is updated in place, its
    source checked again (an item may have arrived since); positions past the end are
    removed. Counts: stored (new), changed, unchanged, removed, unsourced, invalid.
    """
    rows, errors = parse_findings(text)
    stats = {"stored": 0, "changed": 0, "unchanged": 0, "removed": 0, "unsourced": 0}
    stats["invalid"] = len(errors)
    held = {
        r["ordinal"]: r
        for r in _dicts(
            conn.execute(
                "SELECT id, ordinal, claim, value, as_of, source_ref, supersedes, sourced,"
                " source_check FROM findings WHERE note_path = ?",
                (path,),
            )
        )
    }
    for ordinal, row in enumerate(rows):
        sourced, check = check_source(conn, row["source_ref"])
        stats["unsourced"] += not sourced
        values = (
            row["claim"],
            row["value"],
            row["as_of"],
            row["source_ref"],
            row["supersedes"],
            int(sourced),
            check,
            session_id,
            written_at,
        )
        old = held.get(ordinal)
        if old is None:
            conn.execute(
                "INSERT INTO findings (claim, value, as_of, source_ref, supersedes, sourced,"
                " source_check, session_id, written_at, note_path, ordinal, loaded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, path, ordinal, _now()),
            )
            stats["stored"] += 1
            continue
        same_content = tuple(old[k] for k in _FIELDS) == tuple(row[k] for k in _FIELDS)
        if same_content and (old["sourced"], old["source_check"]) == (int(sourced), check):
            stats["unchanged"] += 1
            continue
        conn.execute(
            "UPDATE findings SET claim = ?, value = ?, as_of = ?, source_ref = ?, supersedes = ?,"
            " sourced = ?, source_check = ?, session_id = ?, written_at = ?"
            + ("" if same_content else ", loaded_at = ?")
            + " WHERE id = ?",
            (*values, *(() if same_content else (_now(),)), old["id"]),
        )
        stats["unchanged" if same_content else "changed"] += 1
    stale = [r["id"] for ordinal, r in held.items() if ordinal >= len(rows)]
    if stale:
        conn.execute(f"DELETE FROM findings WHERE id IN ({','.join('?' * len(stale))})", stale)
        stats["removed"] = len(stale)
    return stats


def _row(r: dict) -> dict:
    return {
        "kind": "finding",
        "ref": f"finding:{r['id']}",
        "id": r["id"],
        "claim": r["claim"],
        "value": r["value"],
        "as_of": r["as_of"],
        "source_ref": r["source_ref"],
        "sourced": bool(r["sourced"]),
        "source_check": r["source_check"],
        "supersedes": r["supersedes"],
        "superseded_by": None,
        "note_path": r["note_path"],
        "session_id": r["session_id"],
        "date": r["written_at"],
        "loaded_at": r["loaded_at"],
    }


def _mark_superseded(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Set each row's superseded_by: the newest other finding naming it in `supersedes`,
    as finding:<id> or by its claim, case and accents aside."""
    claims = {r["id"]: search_fold(r["claim"]).strip() for r in rows}
    newer: dict[int, tuple[str, int]] = {}
    for g in _dicts(
        conn.execute("SELECT id, supersedes, written_at FROM findings WHERE supersedes IS NOT NULL")
    ):
        named = (g["supersedes"] or "").strip()
        folded = search_fold(named)
        for r in rows:
            if g["id"] == r["id"]:
                continue
            if named == f"finding:{r['id']}" or (folded and folded == claims[r["id"]]):
                key = (g["written_at"] or "", g["id"])
                if r["id"] not in newer or key > newer[r["id"]]:
                    newer[r["id"]] = key
    for r in rows:
        if r["id"] in newer:
            r["superseded_by"] = f"finding:{newer[r['id']][1]}"


def _topics_of_source(conn: sqlite3.Connection, topic_ids: list[int]) -> str:
    """SQL condition: the finding's source item is tagged with one of `topic_ids`."""
    marks = ",".join("?" * len(topic_ids))
    return f"""(
        ((fd.source_ref GLOB 'email:[0-9]*' OR fd.source_ref GLOB 'news:[0-9]*'
          OR fd.source_ref GLOB 'document:[0-9]*')
         AND CAST(substr(fd.source_ref, instr(fd.source_ref, ':') + 1) AS INTEGER) IN
             (SELECT email_id FROM email_topics WHERE topic_id IN ({marks})))
        OR (fd.source_ref GLOB 'attachment:[0-9]*'
         AND CAST(substr(fd.source_ref, 12) AS INTEGER) IN
             (SELECT a.id FROM attachments a JOIN email_topics et ON et.email_id = a.email_id
              WHERE et.topic_id IN ({marks}))))"""


def findings_matching(
    conn: sqlite3.Connection,
    *,
    topic_ids: Iterable[int] = (),
    words: Iterable[str] = (),
    limit: int = 20,
) -> list[dict]:
    """Findings about a topic, newest first: those whose source item carries one of
    `topic_ids`, and those whose claim or value holds every one of `words` (folded).

    Each row says its kind ('finding'), ref (finding:<id>), whether it is sourced and why
    not, and `superseded_by`: the newer finding that names it, or None.
    """
    ids = [int(t) for t in topic_ids]
    folded = [search_fold(w) for w in words if search_fold(w).strip()]
    if not ids and not folded:
        return []
    try:
        candidates = _dicts(
            conn.execute(
                "SELECT * FROM findings fd"
                + (f" WHERE {_topics_of_source(conn, ids)}" if ids else " WHERE 0"),
                ids + ids,
            )
        )
        if folded:
            seen = {r["id"] for r in candidates}
            for r in _dicts(conn.execute("SELECT * FROM findings")):
                text = search_fold(f"{r['claim']} {r['value'] or ''}")
                if r["id"] not in seen and all(w in text for w in folded):
                    candidates.append(r)
    except sqlite3.OperationalError:  # a store older than v37
        return []
    candidates.sort(key=lambda r: (r["written_at"] or "", r["id"]), reverse=True)
    rows = [_row(r) for r in candidates[: max(int(limit), 0)]]
    _mark_superseded(conn, rows)
    return rows

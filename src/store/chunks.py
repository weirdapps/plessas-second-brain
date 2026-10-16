"""Passages of the long PDF, Word and PowerPoint attachments, for chunk vectors.

One vector per summary put a 100-page document into semantic search as one summary of
about 400 characters, so a question answered on page 60 could reach it only by its words.
An attachment of those three kinds whose text exceeds CHUNK_MIN_CHARS is cut here into
chunks of about CHUNK_CHARS characters, each ending at the best boundary in its last
quarter (a slide marker, a blank line, which is how a PDF's pages and a document's
paragraphs are joined, a line end, a sentence end, a space) and starting CHUNK_OVERLAP
characters before the last one ended. attachment_chunks (schema v39) records where each
chunk lies in the text; src/store/embeddings.py embeds each one, prefixed with the
filename, the email's subject and date and the summary already paid for, under its own
id range, and folds a chunk's match onto its attachment.

Measured on the reference replica, 2026-10-16: 5,733 PDF, Word and PowerPoint
attachments over 8,000 characters, 305 M characters, about 83,000 chunks; over 20,000
characters, 3,521 and 75,000 chunks. 7 of the 11 gold attachments of those kinds in the
owner's retrieval qa set are 8,000 to 20,000 characters long (5 of them decks), so the
threshold is 8,000. Spreadsheets (1.94 G characters) need another treatment, and mail
bodies are left whole: their quoted history repeats down every thread.
"""

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from src.store.stamps import instant, utc_now

CHUNK_MIN_CHARS = 8_000
CHUNK_CHARS = 4_000
CHUNK_OVERLAP = 200
# A text is cut into at most this many chunks: 999 keeps chunk_no below the id stride
# (embeddings.CHUNK_STRIDE). Extraction caps a text at 2 M characters, about 530 chunks.
CHUNK_MAX_PER_ATTACHMENT = 999

_KIND_OF_EXTENSION = {
    ".pdf": "pdf",
    ".doc": "word",
    ".docx": "word",
    ".docm": "word",
    ".dotx": "word",
    ".rtf": "word",
    ".odt": "word",
    ".ppt": "powerpoint",
    ".pptx": "powerpoint",
    ".pptm": "powerpoint",
    ".ppsx": "powerpoint",
    ".odp": "powerpoint",
}
_KIND_OF_MIME = {
    "application/pdf": "pdf",
    "application/msword": "word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "application/rtf": "word",
    "application/vnd.oasis.opendocument.text": "word",
    "application/vnd.ms-powerpoint": "powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "powerpoint",
    "application/vnd.oasis.opendocument.presentation": "powerpoint",
}

# The boundaries a chunk may end at, best first, each as (marker, where the chunk ends
# relative to the marker's start): before a slide marker, after a blank line, after a
# line end, after a sentence end, after a space.
_BOUNDARIES = (("\n--- Slide ", 1), ("\n\n", 2), ("\n", 1), (". ", 2), (" ", 1))


def chunk_kind(filename: str | None, mime_type: str | None) -> str | None:
    """'pdf', 'word' or 'powerpoint' for an attachment whose passages get vectors, else None.
    By the filename's extension, else by its mime type."""
    kind = _KIND_OF_EXTENSION.get(PurePosixPath((filename or "").lower()).suffix)
    return kind or _KIND_OF_MIME.get((mime_type or "").split(";")[0].strip().lower())


def _cut(text: str, start: int) -> int:
    """Where the chunk that starts at `start` ends: at the best boundary in its last quarter,
    else at CHUNK_CHARS."""
    low, high = start + CHUNK_CHARS * 3 // 4, start + CHUNK_CHARS
    for marker, after in _BOUNDARIES:
        found = text.rfind(marker, low - after, high - after)
        if found != -1 and low <= found + after <= high:
            return found + after
    return high


def _next_start(text: str, end: int, start: int) -> int:
    """Where the chunk after one ending at `end` starts: CHUNK_OVERLAP characters back, moved
    on to the start of a word, and always past `start`."""
    back = max(end - CHUNK_OVERLAP, start + 1)
    for i in range(back, end):
        if text[i - 1].isspace() and not text[i].isspace():
            return i
    return end


def split_text(text: str) -> list[tuple[int, int]]:
    """The [start, end) spans of `text`'s chunks, in order, at most CHUNK_MAX_PER_ATTACHMENT.

    A text of up to a quarter more than CHUNK_CHARS is one chunk, as is the tail of a longer
    one, so no chunk stands alone with a few lines.
    """
    spans: list[tuple[int, int]] = []
    start, n = 0, len(text)
    while start < n and len(spans) < CHUNK_MAX_PER_ATTACHMENT:
        end = n if n - start <= CHUNK_CHARS * 5 // 4 else _cut(text, start)
        spans.append((start, end))
        if end >= n:
            break
        start = _next_start(text, end, start)
    return spans


@dataclass
class ChunkPlan:
    """What attachment_chunks holds after plan_chunks.

    spans: attachment id -> [(chunk_no, start, end)] for every eligible attachment whose
        chunks are current; content: attachment id -> its attachment_content id, which the
        chunk vectors' ids are made from; changed: the attachments chunked in this pass,
        whose earlier chunk vectors are stale; deferred: the eligible attachments the
        deadline left unchunked, for the next pass.
    """

    spans: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)
    content: dict[int, int] = field(default_factory=dict)
    changed: set[int] = field(default_factory=set)
    deferred: int = 0


# The summarised attachments whose text is long enough, from the two covering indexes of
# v32, so no stored text is read (src/store/schema.py ATTACHMENT_CONTENT_INDEXES). The plain
# form serves a store without them.
_ELIGIBLE_SQL = """
    SELECT a.id, t.id, a.filename, a.mime_type, t.extracted_at, length(t.extracted_text)
    FROM attachment_content s {summary_index}
    JOIN attachment_content t {sweep_index} ON t.attachment_id = s.attachment_id
    JOIN attachments a ON a.id = s.attachment_id
    WHERE s.llm_status = 'extracted' AND length(s.summary) > 0
      AND length(t.extracted_text) > ?
"""


def eligible(conn: sqlite3.Connection) -> list[tuple]:
    """(attachment id, content id, extracted_at, text length) of each attachment to chunk."""
    indexed = _ELIGIBLE_SQL.format(
        summary_index="INDEXED BY idx_attachment_content_summary_length",
        sweep_index="INDEXED BY idx_attachment_content_sweep",
    )
    try:
        rows = conn.execute(indexed, (CHUNK_MIN_CHARS,)).fetchall()
    except sqlite3.OperationalError as e:
        if "no such index" not in str(e):
            raise
        plain = _ELIGIBLE_SQL.format(summary_index="", sweep_index="")
        rows = conn.execute(plain, (CHUNK_MIN_CHARS,)).fetchall()
    return [(r[0], r[1], r[4], r[5]) for r in rows if chunk_kind(r[2], r[3]) is not None]


def _key(content_id: int, text_chars: int, extracted_at) -> tuple:
    """What says a text is the one its chunks were cut from: its content row, its length and
    when it was read, to the second, as an instant. `maint utc-stamps` rewrites a local stamp in
    UTC and drops its last digit, which must not send every document back to be cut."""
    when = instant(extracted_at)
    return content_id, text_chars, when.replace(microsecond=0) if when else extracted_at


def _stored(conn: sqlite3.Connection) -> dict[int, dict]:
    """attachment id -> {key: _key of the text cut, spans} as stored."""
    out: dict[int, dict] = {}
    for aid, cid, text_chars, extracted_at, no, start, end in conn.execute(
        "SELECT attachment_id, content_id, text_chars, text_extracted_at, chunk_no, "
        "start_char, end_char FROM attachment_chunks ORDER BY attachment_id, chunk_no"
    ):
        entry = out.setdefault(aid, {"key": _key(cid, text_chars, extracted_at), "spans": []})
        entry["spans"].append((no, start, end))
    return out


def _write(conn: sqlite3.Connection, aid: int, cid: int, extracted_at, text: str) -> list:
    spans = [(no, start, end) for no, (start, end) in enumerate(split_text(text))]
    now = utc_now()
    conn.execute("DELETE FROM attachment_chunks WHERE attachment_id = ?", (aid,))
    conn.executemany(
        "INSERT INTO attachment_chunks (attachment_id, chunk_no, content_id, start_char, "
        "end_char, chars, text_chars, text_extracted_at, chunked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (aid, no, cid, start, end, end - start, len(text), extracted_at, now)
            for no, start, end in spans
        ],
    )
    return spans


def plan_chunks(conn: sqlite3.Connection, *, deadline: float | None = None) -> ChunkPlan:
    """Bring attachment_chunks level with the store, and say what it holds.

    An eligible attachment (chunk_kind, summarised, text over CHUNK_MIN_CHARS) with no rows,
    or whose rows were cut from another text (its content row, length or extraction time
    differ), is chunked again, newest first, one commit each, until `deadline` (a
    time.monotonic() value) passes; the rest count as deferred. Rows of an attachment no
    longer eligible are deleted, and so are the stale rows of one deferred, so that no row
    stands for a text the store no longer holds. Reads a text only to cut it.
    """
    plan = ChunkPlan()
    stored = _stored(conn)
    found = eligible(conn)
    wanted = {aid for aid, *_ in found}
    gone = [aid for aid in stored if aid not in wanted]
    todo = []
    for aid, cid, extracted_at, n in found:
        plan.content[aid] = cid
        entry = stored.get(aid)
        if entry is not None and entry["key"] == _key(cid, n, extracted_at):
            plan.spans[aid] = entry["spans"]
        else:
            todo.append((aid, cid, extracted_at, entry is not None))
    for aid, cid, extracted_at, had_rows in sorted(todo, reverse=True):
        if deadline is not None and time.monotonic() >= deadline:
            plan.deferred += 1
            if had_rows:
                gone.append(aid)
            continue
        row = conn.execute(
            "SELECT extracted_text FROM attachment_content WHERE id = ?", (cid,)
        ).fetchone()
        plan.spans[aid] = _write(conn, aid, cid, extracted_at, (row[0] if row else "") or "")
        plan.changed.add(aid)
        conn.commit()
    if gone:
        conn.executemany(
            "DELETE FROM attachment_chunks WHERE attachment_id = ?", [(aid,) for aid in gone]
        )
        conn.commit()
    return plan


def _head(subject, date, summary) -> str:
    """The email's subject and date, then the attachment's summary: what each of its vectors
    is prefixed with."""
    meta = ", ".join(part for part in ((subject or "").strip(), (date or "")[:10]) if part)
    summary = (summary or "").strip()
    return f"{meta}. {summary}" if meta and summary else meta or summary


def attachment_head(conn: sqlite3.Connection, attachment_id: int) -> tuple[str, str]:
    """(filename, head) of an attachment, for the texts of its vectors."""
    row = conn.execute(
        "SELECT a.filename, e.subject, e.date_received, ac.summary FROM attachments a "
        "LEFT JOIN emails e ON e.id = a.email_id "
        "LEFT JOIN attachment_content ac ON ac.attachment_id = a.id WHERE a.id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None:
        return "", ""
    return row[0] or "", _head(row[1], row[2], row[3])


def chunk_texts(
    conn: sqlite3.Connection, attachment_id: int, spans: list[tuple[int, int, int]]
) -> list[str]:
    """The text each of an attachment's chunk vectors is made from, in the order of `spans`:
    'title: <filename> | text: <subject>, <date>. <summary>' and the passage. The attachment's
    text is read once for all of them."""
    filename, head = attachment_head(conn, attachment_id)
    row = conn.execute(
        "SELECT extracted_text FROM attachment_content WHERE attachment_id = ?",
        (attachment_id,),
    ).fetchone()
    text = (row[0] if row else "") or ""
    title = filename or "none"
    return [f"title: {title} | text: {head}\n\n{text[start:end]}" for _no, start, end in spans]

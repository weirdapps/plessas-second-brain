"""Findings a research session files back, parsed from a ```findings block in a note it wrote.

Research teammates make most of the brain's calls, and what they verify (a figure
reconciled, a claim corrected) ended in hand-edited memory files or was lost. A note a
session writes is already ingested as a text-only document (src/export/session_notes.py);
a fenced block in it now also loads, line by line, into the `findings` table (schema v37):

    ```findings
    {"claim": "Share of loan applications made online", "value": "64.0%", "as_of": "2026-08",
     "source_ref": "attachment:1234", "quote": "online share 64.0% in August"}
    ```

One JSON object a line (the example is wrapped here only for width), with these keys and
no other: `claim` (required), `value`, `as_of`, `source_ref` (required), `quote` and
`supersedes`. The agents that write notes read third-party mail, so a mail can make one
write a block. Every line therefore fails closed: it is `accepted`, and only then shown or
used, when all of this holds; otherwise it is stored `rejected` with the reason, never
shown, and listed by `brain findings` for review.

- It parses as one JSON object with known keys only, each of its type and length; `as_of`
  is a date or period (YYYY-MM-DD, YYYY-MM, YYYY-Qn, YYYY) not in the future.
- `source_ref` names a primary item the store holds, as a row's `kind:ref` (email, news,
  document, attachment, teams, whatsapp, calendar). A finding, a topic page, a session or a
  session note is never a source, so no finding rests on another.
- The claim can be checked against that item's stored text: the `quote` (12 characters
  or more), or else the `value` (3 letters or digits or more, whole), occurs in it, case,
  accents and spacing aside; a value given with a quote must occur in one of the two.
- `supersedes`, when given, names an accepted finding (`finding:<id>`) with the same claim
  whose source is older, written by a session of the same rank or below: an agent's
  finding never supersedes the owner's. It marks that finding superseded and nothing
  else: no primary item, fact or decision is ever hidden by a finding.
- Any error while checking a line rejects it, with a log line; none is accepted by default.

Each finding records whether the owner's own session wrote its note or an agent's (a
teammate's or a subagent's), read off the transcript alone (session_notes.author_kind),
and an agent's ranks below the owner's; an unknown author is an agent. Nothing relaxes
these checks, and nothing promotes a rejected finding: the note is corrected and read
again. Findings are derived
and rank below every primary item: nothing indexes or embeds them, and a topic page cites
one only where no primary row says otherwise.
"""

import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime

from src.store.greek import search_fold

_log = logging.getLogger(__name__)

# The fence that opens a block: ```findings (or ~~~findings) on a line of its own.
_BLOCK = re.compile(
    r"^(?P<fence>```|~~~)[ \t]*findings[ \t]*\n(?P<body>.*?)^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
KEYS = ("claim", "value", "as_of", "source_ref", "quote", "supersedes")
CLAIM_CHARS = (3, 300)
VALUE_CHARS = 100
QUOTE_CHARS = (12, 300)
_REF = re.compile(r"^(?P<kind>[a-z_]+):(?P<ref>[0-9]{1,12})$")
_SUPERSEDES = re.compile(r"^finding:(?P<id>[0-9]{1,12})$")
_PERIOD = re.compile(r"^(?P<y>\d{4})(?:-(?P<m>\d{2})(?:-(?P<d>\d{2}))?|-Q(?P<q>[1-4]))?$")
# The most of an item's text a check reads.
_SOURCE_CHARS = 5_000_000

ACCEPTED = "accepted"
REJECTED = "rejected"
OWNER = "owner"
AGENT = "agent"

# Each primary kind: how its text and date are read, by its id: (title, text, source
# class, date, the text of its attachments).
_EMAIL_TEXT = """SELECT e.subject, e.content, e.source_class, e.date_received,
    (SELECT group_concat(ac.extracted_text, char(10)) FROM attachments a
     JOIN attachment_content ac ON ac.attachment_id = a.id WHERE a.email_id = e.id)
    FROM emails e WHERE e.id = ?"""
_SOURCES = {
    "email": _EMAIL_TEXT,
    "news": _EMAIL_TEXT,
    "document": _EMAIL_TEXT,
    "attachment": """SELECT a.filename, ac.extracted_text, e.source_class, e.date_received, NULL
        FROM attachments a LEFT JOIN attachment_content ac ON ac.attachment_id = a.id
        LEFT JOIN emails e ON e.id = a.email_id WHERE a.id = ?""",
    "teams": """SELECT t.title, (SELECT group_concat(m.content_text, char(10))
        FROM teams_messages m WHERE m.thread_id = t.id), NULL, t.started_at, NULL
        FROM teams_threads t WHERE t.id = ?""",
    "whatsapp": """SELECT t.title, (SELECT group_concat(m.content, char(10))
        FROM whatsapp_messages m WHERE m.thread_id = t.id), NULL, t.started_at, NULL
        FROM whatsapp_threads t WHERE t.id = ?""",
    "calendar": """SELECT subject, body, NULL, start_at, NULL
        FROM calendar_events WHERE id = ?""",
}
# Kinds that are themselves derived, and why each cannot source a finding.
_DERIVED = {
    "finding": "a finding cannot be the source of a finding",
    "topic_page": "a topic page is derived",
    "conversation": "a session is derived, not a primary source",
}
SESSION_NOTE_DERIVED = "a session note is derived"


class Rejected(ValueError):
    """Why a line is not accepted."""


def _squash(text: str | None) -> str:
    """Text as a check compares it: case, accents and final sigma folded, spacing one blank."""
    return " ".join(search_fold(text or "").split())


def claim_key(claim: str) -> str:
    """What makes two claims the same claim: their letters and digits, folded."""
    return " ".join(re.sub(r"[^\w]+", " ", search_fold(claim)).split())


def blocks(text: str | None) -> list[tuple[int, int, str]]:
    """(block number, line number, line) for every non-blank line of every block."""
    out = []
    for block_no, match in enumerate(_BLOCK.finditer(text or ""), start=1):
        for line_no, line in enumerate(match.group("body").splitlines(), start=1):
            if line.strip():
                out.append((block_no, line_no, line.strip()))
    return out


def _as_of(value, today: date) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Rejected("as_of is not text")
    match = _PERIOD.match(value.strip())
    if not match:
        raise Rejected("as_of is not YYYY-MM-DD, YYYY-MM, YYYY-Qn or YYYY")
    month = int(match["m"]) if match["m"] else 3 * int(match["q"]) - 2 if match["q"] else 1
    try:
        start = date(int(match["y"]), month, int(match["d"] or 1))
    except ValueError:
        raise Rejected("as_of is not a real date") from None
    if start > today:
        raise Rejected("as_of is in the future")
    return value.strip()


def parse_line(line: str, today: date) -> dict:
    """The fields of one block line, checked for shape alone; Rejected says why not."""
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        raise Rejected("not one JSON object") from None
    if not isinstance(item, dict):
        raise Rejected("not one JSON object")
    unknown = set(item) - set(KEYS)
    if unknown:
        raise Rejected(f"{len(unknown)} unknown field(s)")
    claim = item.get("claim")
    if not isinstance(claim, str) or not CLAIM_CHARS[0] <= len(claim.strip()) <= CLAIM_CHARS[1]:
        raise Rejected(f"claim must be text of {CLAIM_CHARS[0]} to {CLAIM_CHARS[1]} characters")
    value = item.get("value")
    if isinstance(value, bool) or not (value is None or isinstance(value, str | int | float)):
        raise Rejected("value must be text or a number")
    if isinstance(value, int | float):
        value = json.dumps(value)
    if value is not None and not 1 <= len(value.strip()) <= VALUE_CHARS:
        raise Rejected(f"value must be 1 to {VALUE_CHARS} characters")
    quote = item.get("quote")
    if quote is not None and (
        not isinstance(quote, str) or not QUOTE_CHARS[0] <= len(quote.strip()) <= QUOTE_CHARS[1]
    ):
        raise Rejected(f"quote must be text of {QUOTE_CHARS[0]} to {QUOTE_CHARS[1]} characters")
    ref = item.get("source_ref")
    if not isinstance(ref, str) or not ref.strip():
        raise Rejected("no source_ref")
    supersedes = item.get("supersedes")
    if supersedes is not None and (
        not isinstance(supersedes, str) or not _SUPERSEDES.match(supersedes.strip())
    ):
        raise Rejected("supersedes must be finding:<id>")
    return {
        "claim": " ".join(claim.split()),
        "value": value.strip() if value is not None else None,
        "as_of": _as_of(item.get("as_of"), today),
        "source_ref": ref.strip(),
        "quote": " ".join(quote.split()) if quote is not None else None,
        "supersedes": supersedes.strip() if supersedes is not None else None,
    }


def read_source(conn: sqlite3.Connection, ref: str) -> tuple[str, str | None]:
    """The stored text and date of the primary item `ref` names; Rejected when it names
    none, a derived one, or one the store does not hold."""
    kind = ref.partition(":")[0]
    if kind in _DERIVED:
        raise Rejected(_DERIVED[kind])
    match = _REF.match(ref)
    if not match:
        raise Rejected("source_ref is not kind:ref with an integer ref")
    if kind not in _SOURCES:
        raise Rejected("unknown kind")
    row = conn.execute(_SOURCES[kind], (int(match["ref"]),)).fetchone()
    if row is None:
        raise Rejected("not in the store")
    title, text, source_class, when, attached = row
    if source_class == "session_note":
        raise Rejected(SESSION_NOTE_DERIVED)
    whole = "\n".join(part for part in (title, text, attached) if part)
    if not whole.strip():
        raise Rejected("the source holds no text to check against")
    return whole[:_SOURCE_CHARS], when


def _occurs(needle: str, haystack: str) -> bool:
    """Whether `needle` occurs in `haystack` (both squashed) as a whole: not inside a word
    or a number, so '12%' is not found in '112%'."""
    start = haystack.find(needle)
    while start != -1:
        before = haystack[start - 1] if start else " "
        end = start + len(needle)
        after = haystack[end] if end < len(haystack) else " "
        if not (before.isalnum() and needle[0].isalnum()) and not (
            after.isalnum() and needle[-1].isalnum()
        ):
            return True
        start = haystack.find(needle, start + 1)
    return False


def check_against(fields: dict, text: str) -> None:
    """Rejected unless the quote, or else the value, occurs in the source's text."""
    source = _squash(text)
    quote = _squash(fields["quote"]) if fields["quote"] else ""
    value = _squash(fields["value"]) if fields["value"] else ""
    if quote:
        if not _occurs(quote, source):
            raise Rejected("the quote is not in the source")
        if value and not (_occurs(value, quote) or _occurs(value, source)):
            raise Rejected("the value is in neither the quote nor the source")
        return
    if not value:
        raise Rejected("nothing to check against the source: give a value or a quote")
    if sum(c.isalnum() for c in value) < 3:
        raise Rejected("the value is too short to check: give a quote")
    if not _occurs(value, source):
        raise Rejected("the value is not in the source")


# An author's rank: a finding supersedes only one of the same rank or below.
_RANK = {OWNER: 1, AGENT: 0}


def may_supersede(newer: dict, older: dict) -> str | None:
    """Why `newer` may not supersede `older` (both finding rows), or None when it may: the
    same claim, an older source, and an author of the same rank or above, so an agent's
    finding never supersedes the owner's. An unknown author ranks as an agent."""
    if older.get("status") != ACCEPTED:
        return "supersedes names no accepted finding"
    if older.get("id") is not None and older.get("id") == newer.get("id"):
        return "a finding cannot supersede itself"
    if older.get("claim_key") != newer.get("claim_key"):
        return "supersedes names a finding with another claim"
    if not (older.get("source_date") and newer.get("source_date")):
        return "supersedes names a finding whose source is not older"
    if not older["source_date"] < newer["source_date"]:
        return "supersedes names a finding whose source is not older"
    if _RANK.get(str(newer.get("author_kind")), 0) < _RANK.get(str(older.get("author_kind")), 0):
        return "an agent's finding cannot supersede the owner's"
    return None


def _check_supersedes(conn: sqlite3.Connection, fields: dict, newer: dict) -> None:
    if not fields["supersedes"]:
        return
    target_id = int(fields["supersedes"].partition(":")[2])
    rows = _dicts(conn.execute("SELECT * FROM findings WHERE id = ?", (target_id,)))
    if len(rows) != 1:
        raise Rejected("supersedes names no accepted finding")
    why = may_supersede(newer, rows[0])
    if why:
        raise Rejected(why)


def validate(
    conn: sqlite3.Connection,
    line: str,
    today: date,
    *,
    author_kind: str = AGENT,
    finding_id: int | None = None,
) -> tuple[dict, str, str]:
    """(fields, status, reason) for one block line, written by a session of `author_kind`
    (and held as `finding_id`, when it is). Never raises: anything that goes wrong while
    checking rejects the line."""
    fields: dict = dict.fromkeys(KEYS)
    fields["source_date"] = None
    try:
        fields.update(parse_line(line, today))
        text, when = read_source(conn, fields["source_ref"])
        fields["source_date"] = when
        check_against(fields, text)
        newer = {
            "id": finding_id,
            "claim_key": claim_key(fields["claim"]),
            "source_date": when,
            "author_kind": author_kind,
        }
        _check_supersedes(conn, fields, newer)
    except Rejected as e:
        return fields, REJECTED, str(e)
    except Exception as e:  # fail closed: an error is a rejection, never an acceptance
        return fields, REJECTED, f"validation error: {type(e).__name__}"
    return fields, ACCEPTED, "ok"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """The rows as dicts, whatever row factory the connection has."""
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


_STORED = ("claim", "value", "as_of", "source_ref", "quote", "supersedes")


def sync_note_findings(
    conn: sqlite3.Connection,
    path: str,
    session_id: str | None,
    written_at: str | None,
    text: str | None,
    *,
    author_kind: str = AGENT,
    today: date | None = None,
) -> dict:
    """Make the findings of note `path` those its text holds now, each checked again.
    Does not commit.

    One row per line of the note's blocks, by position: an unchanged one keeps its id and
    loaded_at, so a page citing it stays valid, and is checked again (its source may have
    arrived since); positions past the end are removed. A rejected line is stored with
    its reason and logged. `author_kind` is 'owner' for the owner's own session; anything
    else is an agent's. Counts: lines, accepted, rejected, new, changed, removed.
    """
    today = today or datetime.now(UTC).date()
    author = OWNER if author_kind == OWNER else AGENT
    held = {
        r["ordinal"]: r
        for r in _dicts(conn.execute("SELECT * FROM findings WHERE note_path = ?", (path,)))
    }
    lines = blocks(text)
    stats = dict.fromkeys(("lines", "accepted", "rejected", "new", "changed", "removed"), 0)
    stats["lines"] = len(lines)
    for ordinal, (block_no, line_no, line) in enumerate(lines):
        old = held.get(ordinal)
        fields, status, reason = validate(
            conn, line, today, author_kind=author, finding_id=old["id"] if old else None
        )
        stats[status] += 1
        if status == REJECTED:
            _log.warning(
                "finding rejected: %s block %d line %d: %s", path, block_no, line_no, reason
            )
        claim = (fields["claim"] or "")[: CLAIM_CHARS[1]]
        values = {
            **{k: fields[k] for k in _STORED},
            "claim": claim,
            "claim_key": claim_key(claim),
            "source_date": fields["source_date"],
            "status": status,
            "reason": reason,
            "session_id": session_id,
            "author_kind": author,
            "written_at": written_at,
        }
        if old is None:
            columns = ", ".join([*values, "note_path", "ordinal", "loaded_at"])
            marks = ", ".join("?" * (len(values) + 3))
            conn.execute(
                f"INSERT INTO findings ({columns}) VALUES ({marks})",
                (*values.values(), path, ordinal, _now()),
            )
            stats["new"] += 1
            continue
        if all(old[k] == v for k, v in values.items()):
            continue
        same_content = all(old[k] == values[k] for k in _STORED)
        assignments = ", ".join(f"{k} = ?" for k in values)
        conn.execute(
            f"UPDATE findings SET {assignments}"
            + ("" if same_content else ", loaded_at = ?")
            + " WHERE id = ?",
            (*values.values(), *(() if same_content else (_now(),)), old["id"]),
        )
        stats["changed"] += not same_content
    stale = [r["id"] for ordinal, r in held.items() if ordinal >= len(lines)]
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
        "source_date": r["source_date"],
        "quote": r["quote"],
        "status": r["status"],
        "reason": r["reason"],
        "author_kind": r["author_kind"],
        "supersedes": r["supersedes"],
        "superseded_by": None,
        "note_path": r["note_path"],
        "ordinal": r["ordinal"],
        "session_id": r["session_id"],
        "date": r["written_at"],
        "loaded_at": r["loaded_at"],
    }


def _mark_superseded(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Set each row's superseded_by: the newest accepted finding naming it in `supersedes`
    that may supersede it (may_supersede, checked again here: an author or a status may
    have changed since the line was checked)."""
    by_id = {r["id"]: r for r in rows}
    held = {
        r["id"]: r
        for r in _dicts(
            conn.execute(
                "SELECT id, status, claim_key, source_date, author_kind FROM findings"
                " WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(by_id)),),
            )
        )
    }
    newer: dict[int, tuple[str, int]] = {}
    for g in _dicts(
        conn.execute(
            "SELECT id, supersedes, status, claim_key, source_date, author_kind FROM findings"
            " WHERE status = ? AND supersedes IS NOT NULL",
            (ACCEPTED,),
        )
    ):
        target = int(str(g["supersedes"]).partition(":")[2] or 0)
        if target in held and may_supersede(g, held[target]) is None:
            key = (g["source_date"] or "", g["id"])
            if target not in newer or key > newer[target]:
                newer[target] = key
    for r in rows:
        if r["id"] in newer:
            r["superseded_by"] = f"finding:{newer[r['id']][1]}"


def _topics_of_source(topic_ids: list[int]) -> str:
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


def _rank(rows: list[dict]) -> list[dict]:
    """The owner's own first, then agents', each newest first."""
    rows = sorted(rows, key=lambda r: (r["written_at"] or "", r["id"]), reverse=True)
    return sorted(rows, key=lambda r: r["author_kind"] != OWNER)


def findings_matching(
    conn: sqlite3.Connection,
    *,
    topic_ids: Iterable[int] = (),
    words: Iterable[str] = (),
    limit: int = 20,
) -> list[dict]:
    """The accepted findings about a topic: those whose source item carries one of
    `topic_ids`, and those whose claim or value holds every one of `words` (folded).

    The owner's own first, then agents', each newest first. A rejected finding is never
    returned (`rejected_findings` lists those for review). Each row says its kind
    ('finding'), ref (finding:<id>), author_kind and `superseded_by`.
    """
    ids = [int(t) for t in topic_ids]
    folded = [search_fold(w) for w in words if search_fold(w).strip()]
    if not ids and not folded:
        return []
    try:
        candidates = _dicts(
            conn.execute(
                "SELECT * FROM findings fd WHERE status = ?"
                + (f" AND {_topics_of_source(ids)}" if ids else " AND 0"),
                [ACCEPTED, *ids, *ids],
            )
        )
        if folded:
            seen = {r["id"] for r in candidates}
            for r in _dicts(conn.execute("SELECT * FROM findings WHERE status = ?", (ACCEPTED,))):
                text = search_fold(f"{r['claim']} {r['value'] or ''}")
                if r["id"] not in seen and all(w in text for w in folded):
                    candidates.append(r)
    except sqlite3.OperationalError:  # a store older than v37
        return []
    rows = [_row(r) for r in _rank(candidates)[: max(int(limit), 0)]]
    _mark_superseded(conn, rows)
    return rows


def listed(conn: sqlite3.Connection, status: str, limit: int = 100) -> list[dict]:
    """The findings of one status, ranked as findings_matching ranks them: for review."""
    try:
        rows = _dicts(conn.execute("SELECT * FROM findings WHERE status = ?", (status,)))
    except sqlite3.OperationalError:
        return []
    out = [_row(r) for r in _rank(rows)[: max(int(limit), 0)]]
    _mark_superseded(conn, out)
    return out


def counts(conn: sqlite3.Connection) -> dict:
    """Findings held, by status and by who wrote their note."""
    try:
        rows = conn.execute(
            "SELECT status, author_kind, COUNT(*) FROM findings GROUP BY status, author_kind"
            " ORDER BY status, author_kind"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {f"{status} ({author})": n for status, author, n in rows}


def cmd(args) -> int:
    """`brain findings`: the findings held, by status, and the rejected ones with why, for
    review (--accepted lists the accepted). Reads only, so it runs on a replica."""
    from src import config
    from src.store.sql_readonly import connect_read_only

    conn = connect_read_only(args.db, immutable=config.is_pulled_copy())
    try:
        held = counts(conn)
        rows = listed(conn, ACCEPTED if args.accepted else REJECTED, args.limit)
    finally:
        conn.close()
    print("findings: " + (", ".join(f"{k} {v}" for k, v in held.items()) or "none held"))
    for r in rows:
        print(
            f"  {r['ref']} {r['status']}, {r['author_kind']} session, {r['note_path']}"
            f" entry {r['ordinal'] + 1}, {r['date']}: {r['reason']}; source {r['source_ref']};"
            f" claim {r['claim'][:100]!r}"
            + (f"; superseded by {r['superseded_by']}" if r["superseded_by"] else "")
        )
    return 0

"""Thread identity (issue 179): the gated migration `thread_identity`.

Mail with no Outlook ConversationId (the retired Apple Mail export, 2025-03 to 2026-06) was
threaded by the loader's fallback, `conversation_id_of`: the hash of the first References
id, else of In-Reply-To, else of the normalised subject. Two faults followed. A reply sat
apart from its root, under the hash of an id the store held under another thread; and every
mail of one subject, over any span of time, was one thread (one of 680 emails over 13
months).

The step, on the hard identifiers only (no merge by name, issue 170):

- a legacy reply joins the thread of the earliest message its References (then
  In-Reply-To) names that the store holds, by Message-ID, followed to that message's own
  final thread;
- a subject-only thread is cut at every silence over GAP (14 days): the first run keeps
  the old id, each later run gets `segment_id(old id, its first date)`;
- every legacy email's id before and after is kept in thread_id_map (old_id, new_id, rule),
  so `revert` puts each back, and the loader finds a subject's latest run there. No row is
  deleted.

Outlook threads, news, documents, session notes and the blank-subject thread (which every
reader treats as one email per thread) are left as they are. Once applied, the loader
threads new mail with no ConversationId by the same rules (`thread_id`); before, exactly as
it did. The step also creates thread_rollups (src/store/thread_rollups.py), empty.
"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

from src.store.schema import applied_gated, normalize_subject, subject_to_conversation_id

NAME = "thread_identity"
GAP = timedelta(days=14)
# Classes that are threads of mail. News is a day per pipeline; documents and session notes
# have no thread.
THREADED_CLASSES = ("mail", "automation")
RULES = ("reference", "subject_gap", "kept")

MAP_TABLE = """CREATE TABLE IF NOT EXISTS thread_id_map (
    email_id INTEGER PRIMARY KEY REFERENCES emails(id),
    old_id TEXT NOT NULL,
    new_id TEXT NOT NULL,
    rule TEXT NOT NULL CHECK (rule IN ('reference', 'subject_gap', 'kept')),
    mapped_at TEXT NOT NULL
)"""
MAP_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_thread_id_map_old ON thread_id_map(old_id)",
    "CREATE INDEX IF NOT EXISTS idx_thread_id_map_new ON thread_id_map(new_id)",
)

_BLANK = subject_to_conversation_id("")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def enabled(conn: sqlite3.Connection) -> bool:
    """Whether this store has taken the step."""
    return NAME in applied_gated(conn)


def segment_id(base: str, first_date: str) -> str:
    """The id of a run of a subject thread after a silence: its base id and its first date."""
    return hashlib.sha256(f"{base}|{first_date}".encode()).hexdigest()[:16]


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _tokens(references: str | None, in_reply_to: str | None) -> list[str]:
    out = (references or "").split()
    if in_reply_to and in_reply_to.strip():
        out.append(in_reply_to.strip())
    return out


def fallback_id(metadata: dict) -> str:
    """The loader's id for a staged email (src/store/loader.py conversation_id_of): Outlook's
    ConversationId, else the hash of the first References id, of In-Reply-To, of the
    normalised subject."""
    outlook = metadata.get("conversation_id", "")
    if outlook:
        return outlook
    references = metadata.get("references", "") or ""
    in_reply_to = metadata.get("in_reply_to", "") or ""
    if references:
        root = references.strip().split()[0]
        return hashlib.sha256(root.encode()).hexdigest()[:16]
    if in_reply_to:
        return hashlib.sha256(in_reply_to.strip().encode()).hexdigest()[:16]
    return subject_to_conversation_id(metadata.get("subject", "") or "")


def legacy_id(subject: str | None, references: str | None, in_reply_to: str | None) -> str:
    """The id the loader's fallback gives a message with no ConversationId."""
    return fallback_id(
        {"subject": subject or "", "references": references or "", "in_reply_to": in_reply_to}
    )


# --- the step ----------------------------------------------------------------------------


def plan(conn: sqlite3.Connection) -> dict[int, tuple[str, str, str]]:
    """{email_id: (old_id, new_id, rule)} for every legacy email: one whose stored id is the
    loader's fallback id, of a threaded class, outside the blank-subject thread."""
    rows = conn.execute(
        'SELECT id, date_received, subject, "references", in_reply_to, internet_message_id,'
        " conversation_id, COALESCE(source_class, 'mail'), COALESCE(mailbox_name, '')"
        " FROM emails"
    ).fetchall()
    stored: dict[int, str] = {}
    by_mid: dict[str, int] = {}
    replies: dict[int, list[str]] = {}
    subjects: dict[str, list[tuple[datetime | None, int, str]]] = {}
    for eid, date, subject, refs, irt, mid, conv, cls, mailbox in rows:
        threaded = cls in THREADED_CLASSES and mailbox != "News" and bool((conv or "").strip())
        if mid and threaded:
            by_mid[mid] = eid
        if not threaded:
            continue
        stored[eid] = conv
        if conv == _BLANK or conv != legacy_id(subject, refs, irt):
            continue
        tokens = _tokens(refs, irt)
        if tokens:
            replies[eid] = tokens
        else:
            subjects.setdefault(conv, []).append((_when(date), eid, date or ""))

    new: dict[int, str] = {}
    rule: dict[int, str] = {}
    for base, members in subjects.items():
        members.sort(key=lambda m: (m[0] or datetime.min.replace(tzinfo=UTC), m[1]))
        current, last = base, None
        for when, eid, raw in members:
            if last is not None and when is not None and when - last > GAP:
                current = segment_id(base, raw)
            if when is not None:
                last = when
            new[eid] = current
            rule[eid] = "kept" if current == base else "subject_gap"

    def settle(eid: int) -> None:
        """Follow references from reply to reply until a message whose thread is known (a
        subject run, an Outlook thread, a settled reply, or one naming nothing held), and
        give every reply on the way that thread."""
        path: list[int] = []
        node = eid
        while True:
            if node in new:
                result = new[node]
                break
            if node not in replies or node in path:
                result = stored.get(node, "")
                break
            path.append(node)
            target = next(
                (by_mid[t] for t in replies[node] if t in by_mid and by_mid[t] != node), None
            )
            if target is None:
                result = stored[node]
                break
            node = target
        for p in path:
            # A reply whose root is the blank-subject thread keeps its own id.
            new[p] = result if result and result != _BLANK else stored[p]
            rule[p] = "reference" if new[p] != stored[p] else "kept"

    for eid in replies:
        if eid not in new:
            settle(eid)
    return {eid: (stored[eid], new[eid], rule[eid]) for eid in new}


def migrate(conn: sqlite3.Connection) -> None:
    """Gated migration thread_identity: the map, every legacy email re-threaded, and the
    rollup table. The caller commits (schema.apply_gated), or rolls back for a dry run.
    Re-threading writes emails.conversation_id alone, which no full-text trigger watches."""
    from src.store import thread_rollups

    conn.execute(MAP_TABLE)
    for sql in MAP_INDEXES:
        conn.execute(sql)
    thread_rollups.create(conn)
    planned = plan(conn)
    at = _now()
    conn.executemany(
        "INSERT OR REPLACE INTO thread_id_map (email_id, old_id, new_id, rule, mapped_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(eid, old, new, rule, at) for eid, (old, new, rule) in planned.items()],
    )
    conn.executemany(
        "UPDATE emails SET conversation_id = ? WHERE id = ?",
        [(new, eid) for eid, (old, new, _) in planned.items() if new != old],
    )


def revert(conn: sqlite3.Connection) -> int:
    """Put every mapped email back on its old id and mark the step not applied, so the
    loader threads as before; the emails changed. The map stays as the record. Rollups are
    derived from the ids, so they are cleared. The caller commits."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "thread_id_map" not in tables:
        return 0
    changed = conn.execute(
        "UPDATE emails SET conversation_id = (SELECT m.old_id FROM thread_id_map m"
        " WHERE m.email_id = emails.id) WHERE id IN (SELECT email_id FROM thread_id_map"
        " WHERE old_id <> new_id) AND conversation_id = (SELECT m.new_id FROM thread_id_map m"
        " WHERE m.email_id = emails.id)"
    ).rowcount
    if "gated_migrations" in tables:
        conn.execute("DELETE FROM gated_migrations WHERE name = ?", (NAME,))
    if "thread_rollups" in tables:
        conn.execute("DELETE FROM thread_rollups")
    return changed


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """What the step changes, for the dry run: threads of mail, the largest one, and the map
    by rule."""
    classes = ", ".join(f"'{c}'" for c in THREADED_CLASSES)
    sizes = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE COALESCE(source_class, 'mail') IN"
        f" ({classes}) AND COALESCE(mailbox_name, '') <> 'News'"
        " AND TRIM(COALESCE(conversation_id, '')) <> '' AND conversation_id <> ?"
        " GROUP BY conversation_id",
        (_BLANK,),
    ).fetchall()
    out = {
        "email threads": len(sizes),
        "largest email thread": max((s[0] for s in sizes), default=0),
    }
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "thread_id_map" in tables:
        for rule, n in conn.execute("SELECT rule, COUNT(*) FROM thread_id_map GROUP BY 1"):
            out[f"thread_id_map {rule}"] = n
    if "thread_rollups" in tables:
        out["thread_rollups rows"] = conn.execute("SELECT COUNT(*) FROM thread_rollups").fetchone()[
            0
        ]
    return out


# --- new mail ----------------------------------------------------------------------------


def thread_id(conn: sqlite3.Connection, metadata: dict) -> tuple[str, str | None]:
    """The thread a staged email is stored under, and the map rule to record (None: no map
    row). Outlook's ConversationId, or on a store without the step, the loader's fallback,
    unchanged."""
    legacy = fallback_id(metadata)
    if metadata.get("conversation_id") or not enabled(conn):
        return legacy, None
    tokens = _tokens(metadata.get("references"), metadata.get("in_reply_to"))
    if tokens:
        for token in tokens:
            row = conn.execute(
                "SELECT conversation_id FROM emails WHERE internet_message_id = ?", (token,)
            ).fetchone()
            if row and row[0] and row[0] != _BLANK:
                return row[0], "reference" if row[0] != legacy else "kept"
        return legacy, "kept"
    if legacy == _BLANK or not normalize_subject(metadata.get("subject", "")):
        return legacy, None
    when = _when(metadata.get("date_received"))
    nearest = None
    for new_id, date in conn.execute(
        "SELECT m.new_id, e.date_received FROM thread_id_map m JOIN emails e ON e.id = m.email_id"
        " WHERE m.old_id = ?",
        (legacy,),
    ):
        at = _when(date)
        if when is None or at is None:
            continue
        gap = abs(when - at)
        if nearest is None or gap < nearest[0]:
            nearest = (gap, new_id)
    if nearest is not None and nearest[0] <= GAP:
        chosen = nearest[1]
    elif nearest is not None:
        chosen = segment_id(legacy, str(metadata.get("date_received", "")))
    else:
        chosen = legacy
    return chosen, "kept" if chosen == legacy else "subject_gap"


def record(conn: sqlite3.Connection, email_id: int, metadata: dict, new_id: str, rule: str) -> None:
    """The map row of an email the loader just threaded by the step."""
    conn.execute(
        "INSERT OR REPLACE INTO thread_id_map (email_id, old_id, new_id, rule, mapped_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (email_id, fallback_id(metadata), new_id, rule, _now()),
    )

"""The page layer (issue 169, gated step `page_layer`): compiled, cited pages.

A dossier (person_context, topic_context, meeting_prep) is recomputed per call from 10-14
queries; nothing is compiled ahead of the question. A page is: one per person, topic,
mail thread and meeting series that is active enough to need one, kept in `pages` and
served beside the dossier.

A page has two parts, both cited line by line as [kind:id, date] (get_item reads each):

  skeleton   read by SQL, deterministically, for every entity a sync touched (the
             sync's Step 8c, `refresh`): counts, dates, the newest threads, decisions,
             open actions, meetings. No model.
  synthesis  at most SYNTHESIS_WORDS words a model writes from the skeleton's rows and
             the entity's evidence (src/pipeline/pages.py), only for a page marked dirty;
             a line whose refs were not in that evidence is dropped.

Keys: a person is their people id (an identity, with the gated person_identity step: a row
with a hard identifier; before it, a row with an address), never the owner; a topic is its
canonical topic ("c<id>", gated topic_registry) or, unplaced or without the step, the raw
topic ("t<id>"); a thread its conversation_id (the ids gated thread_identity recomputed,
its rollup when held); a series its calendar recurrence_master_id.

Rules the topic pages (src/store/topic_pages.py) set and this layer keeps: a page is never a
source (no full-text or vector index holds it, and no page is built from another), it is
never edited by hand (corrections are rows of page_overrides, applied when it is read), and
it is rebuilt only from a dirty flag. A full build writes a new generation and `swap`
makes it live in one transaction, so a reader never sees half a rebuild. Forgetting an item
(`invalidate_refs`) blanks every synthesis that cited it and compiles the skeleton again.

Without the step every function here is a no-op that returns nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

NAME = "page_layer"
KINDS = ("person", "topic", "thread", "series")
SYNTHESIS_WORDS = 300

# Who gets a page: activity in the window, a floor, and a cap a kind (the most active
# first), so the set stays a few hundred rows a kind.
WINDOW_DAYS = {"person": 180, "topic": 180, "thread": 90, "series": 365}
FLOOR = {"person": 3, "topic": 5, "thread": 3, "series": 3}
CAP = {"person": 300, "topic": 300, "thread": 300, "series": 100}
# Lines a skeleton section takes.
_LINES = 5
_TEXT = 220

DDL = (
    """CREATE TABLE IF NOT EXISTS pages (
        kind TEXT NOT NULL CHECK (kind IN ('person', 'topic', 'thread', 'series')),
        entity_key TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0,
        display_name TEXT NOT NULL,
        skeleton TEXT NOT NULL,
        skeleton_at TEXT NOT NULL,
        synthesis TEXT,
        evidence_keys TEXT,
        built_at TEXT,
        prompt_hash TEXT,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL,
        words INTEGER,
        lines_dropped INTEGER,
        PRIMARY KEY (kind, entity_key, generation)
    )""",
    """CREATE TABLE IF NOT EXISTS page_generations (
        kind TEXT PRIMARY KEY,
        live INTEGER NOT NULL DEFAULT 0,
        building INTEGER,
        swapped_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS page_dirty (
        kind TEXT NOT NULL,
        entity_key TEXT NOT NULL,
        reason TEXT NOT NULL,
        marked_at TEXT NOT NULL,
        PRIMARY KEY (kind, entity_key)
    )""",
    """CREATE TABLE IF NOT EXISTS page_overrides (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        entity_key TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('hide', 'drop_ref', 'correct')),
        ref TEXT,
        text TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_page_overrides_entity ON page_overrides(kind, entity_key)",
    """CREATE TABLE IF NOT EXISTS page_watermarks (
        source TEXT PRIMARY KEY,
        last_id INTEGER NOT NULL
    )""",
)


def migrate(conn: sqlite3.Connection) -> None:
    """The gated step: empty tables; the first sync's refresh compiles the skeletons."""
    for statement in DDL:
        conn.execute(statement)
    for kind in KINDS:
        conn.execute("INSERT OR IGNORE INTO page_generations (kind, live) VALUES (?, 0)", (kind,))


def enabled(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'page_generations'"
        ).fetchone()
        is not None
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _in(values: Iterable) -> str:
    return json.dumps(list(values))


def _cut(text: str | None, chars: int = _TEXT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= chars else text[: chars - 1] + "…"


def _line(text: str, kind: str, ref: Any, date: str | None) -> dict:
    return {"text": text, "refs": [f"{kind}:{ref}"], "date": (date or "")[:10] or None}


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
        ).fetchone()
        is not None
    )


# --- who gets a page ------------------------------------------------------------------------


def _owner_ids(conn: sqlite3.Connection) -> set[int]:
    from src import config
    from src.store import person_identity
    from src.store.owner import owner_address_sql

    ids = set()
    fixed = person_identity.owner_id(conn)
    if fixed is not None:
        ids.add(fixed)
    condition, params = owner_address_sql("p.email", config.USER_EMAIL_PATTERN)
    ids |= {r[0] for r in conn.execute(f"SELECT id FROM people p WHERE {condition}", params)}
    return ids


def _people_eligible(conn: sqlite3.Connection) -> list[str]:
    from src.store import person_identity

    # An identity: a row with an address, or (with the step) any hard identifier.
    identity = "COALESCE(p.email, '') <> ''"
    if person_identity.active(conn):
        identity = (
            f"({identity} OR p.id IN (SELECT person_id FROM person_identifiers"
            " WHERE kind <> 'name'))"
        )
    rows = conn.execute(
        f"""SELECT ep.person_id, COUNT(DISTINCT ep.email_id) n FROM email_people ep
            JOIN emails e ON e.id = ep.email_id JOIN people p ON p.id = ep.person_id
            WHERE e.date_received >= ? AND e.source_class = 'mail' AND {identity}
            GROUP BY ep.person_id HAVING n >= ? ORDER BY n DESC, ep.person_id""",
        (_since(WINDOW_DAYS["person"]), FLOOR["person"]),
    ).fetchall()
    owner = _owner_ids(conn)
    return [str(pid) for pid, _ in rows if pid not in owner][: CAP["person"]]


def topic_key(conn: sqlite3.Connection, topic_id: int) -> str:
    """A topic's page key: its canonical's when the registry placed it, else its own."""
    from src.store import topic_registry

    canonical = topic_registry.canonical_of(conn, topic_id)
    return f"c{canonical}" if canonical is not None else f"t{topic_id}"


def _topic_members(conn: sqlite3.Connection, key: str) -> list[int]:
    if key.startswith("c"):
        return [
            r[0]
            for r in conn.execute(
                "SELECT topic_id FROM topic_aliases WHERE canonical_id = ? ORDER BY topic_id",
                (int(key[1:]),),
            )
        ]
    return [int(key[1:])]


def _topics_eligible(conn: sqlite3.Connection) -> list[str]:
    from src.store import topic_registry

    since = _since(WINDOW_DAYS["topic"])
    if topic_registry.enabled(conn):
        group = "COALESCE('c' || ta.canonical_id, 't' || et.topic_id)"
        join = "LEFT JOIN topic_aliases ta ON ta.topic_id = et.topic_id"
    else:
        group, join = "'t' || et.topic_id", ""
    rows = conn.execute(
        f"""SELECT {group} k, COUNT(DISTINCT et.email_id) n FROM email_topics et {join}
            JOIN emails e ON e.id = et.email_id
            WHERE e.date_received >= ? AND e.source_class = 'mail'
            GROUP BY k HAVING n >= ? ORDER BY n DESC, k""",
        (since, FLOOR["topic"]),
    ).fetchall()
    return [k for k, _ in rows][: CAP["topic"]]


def _threads_eligible(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT conversation_id, COUNT(*) n, MAX(date_received) last FROM emails
           WHERE source_class = 'mail' AND conversation_id IS NOT NULL AND conversation_id <> ''
           GROUP BY conversation_id HAVING n >= ? AND last >= ?
           ORDER BY last DESC, conversation_id""",
        (FLOOR["thread"], _since(WINDOW_DAYS["thread"])),
    ).fetchall()
    return [cid for cid, _, _ in rows][: CAP["thread"]]


def _series_eligible(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT recurrence_master_id, COUNT(*) n, MAX(start_at) last FROM calendar_events
           WHERE recurrence_master_id IS NOT NULL AND recurrence_master_id <> ''
             AND is_cancelled = 0 AND start_at >= ?
           GROUP BY recurrence_master_id HAVING n >= ? ORDER BY last DESC""",
        (_since(WINDOW_DAYS["series"]), FLOOR["series"]),
    ).fetchall()
    return [m for m, _, _ in rows][: CAP["series"]]


def eligible(conn: sqlite3.Connection, kind: str) -> list[str]:
    """The entity keys of `kind` that get a page, the most active first, at most CAP."""
    return {
        "person": _people_eligible,
        "topic": _topics_eligible,
        "thread": _threads_eligible,
        "series": _series_eligible,
    }[kind](conn)


# --- skeletons ------------------------------------------------------------------------------


# Current items only: no duplicate, superseded or invalid one. The same test as
# consolidate.current_sql (state columns since v35, which a store with this gated step
# holds); written out because consolidate imports the extraction stack.
_CURRENT = "{a}.canonical_id IS NULL AND {a}.superseded_by IS NULL AND {a}.invalid_at IS NULL"


def _current(conn: sqlite3.Connection) -> tuple[str, str]:
    """The conditions that keep current decisions (d) and action items (a) only."""
    return _CURRENT.format(a="d"), _CURRENT.format(a="a")


def _dedupe(lines: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Each section without a line whose words another line already said."""
    from src.store.greek import search_fold

    out: dict[str, list[dict]] = {}
    for section, rows in lines.items():
        seen: set[str] = set()
        out[section] = []
        for line in rows:
            words = " ".join(search_fold(line["text"]).split()[:12])
            if words not in seen:
                seen.add(words)
                out[section].append(line)
    return out


def _visible_mail() -> str:
    return "e.source_class = 'mail'"


def _person_skeleton(conn: sqlite3.Connection, key: str) -> dict | None:
    from src.store.greek import register_sql_functions

    register_sql_functions(conn)
    row = conn.execute("SELECT id, name, email FROM people WHERE id = ?", (int(key),)).fetchone()
    if row is None:
        return None
    pid, name, address = row
    since = _since(WINDOW_DAYS["person"])
    mail = f"""SELECT e.id, e.date_received, e.subject, e.conversation_id FROM emails e
               WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
                 AND {_visible_mail()}"""
    emails = conn.execute(mail + " ORDER BY e.date_received DESC", (pid,)).fetchall()
    recent = [e for e in emails if (e[1] or "") >= since]
    lines: dict[str, list[dict]] = {"threads": [], "decisions": [], "actions": [], "meetings": []}
    seen: set[str] = set()
    for email_id, date, subject, cid in emails:
        if len(lines["threads"]) >= _LINES:
            break
        if cid in seen:
            continue
        seen.add(cid)
        n = sum(1 for e in emails if e[3] == cid)
        lines["threads"].append(
            _line(f"«{_cut(subject, 120)}»: {n} with them", "email", email_id, date)
        )
    folded = name or ""
    cur_d, cur_a = _current(conn)
    for text, email_id, date in conn.execute(
        f"""SELECT d.decision, d.email_id, COALESCE(e.date_received, d.decision_date)
           FROM decisions d JOIN emails e ON e.id = d.email_id
           WHERE sb_fold(d.decided_by) = sb_fold(?) AND e.source_class = 'mail' AND {cur_d}
           ORDER BY e.date_received DESC LIMIT ?""",
        (folded, _LINES),
    ):
        lines["decisions"].append(_line(f"Decided: {_cut(text)}", "email", email_id, date))
    for task, deadline, email_id, date in conn.execute(
        f"""SELECT a.task, a.deadline, a.email_id, e.date_received FROM action_items a
           JOIN emails e ON e.id = a.email_id
           WHERE sb_fold(a.owner) = sb_fold(?) AND COALESCE(a.status, 'open') = 'open'
             AND e.source_class = 'mail' AND {cur_a}
           ORDER BY e.date_received DESC LIMIT ?""",
        (folded, _LINES),
    ):
        due = f" (due {deadline})" if deadline else ""
        lines["actions"].append(_line(f"Owes: {_cut(task)}{due}", "email", email_id, date))
    meet = """SELECT ce.id, ce.start_at, ce.subject FROM calendar_events ce
              WHERE ce.is_cancelled = 0 AND ce.id IN (SELECT event_id FROM event_attendees
                    WHERE person_id = ? OR (? <> '' AND lower(email) = lower(?)))"""
    args = (pid, address or "", address or "")
    nxt = conn.execute(
        meet + " AND ce.start_at >= ? ORDER BY ce.start_at LIMIT 1", (*args, _now())
    ).fetchone()
    last = conn.execute(
        meet + " AND ce.start_at < ? ORDER BY ce.start_at DESC LIMIT 1", (*args, _now())
    ).fetchone()
    if nxt:
        lines["meetings"].append(_line(f"Next meeting: {_cut(nxt[2], 120)}", "calendar", *nxt[:2]))
    if last:
        lines["meetings"].append(
            _line(f"Last meeting: {_cut(last[2], 120)}", "calendar", *last[:2])
        )
    return {
        "name": name or address or key,
        "facts": {
            "address": address,
            "emails": len(emails),
            f"emails_{WINDOW_DAYS['person']}d": len(recent),
            "first_seen": (emails[-1][1] or "")[:10] if emails else None,
            "last_contact": (emails[0][1] or "")[:10] if emails else None,
        },
        "sections": lines,
    }


def _topic_skeleton(conn: sqlite3.Connection, key: str) -> dict | None:
    members = _topic_members(conn, key)
    if not members:
        return None
    if key.startswith("c"):
        named = conn.execute(
            "SELECT display_name FROM canonical_topics WHERE id = ?", (int(key[1:]),)
        ).fetchone()
    else:
        named = conn.execute(
            "SELECT COALESCE(display_name, name) FROM topics WHERE id = ?", (members[0],)
        ).fetchone()
    if named is None:
        return None
    tagged = """e.id IN (SELECT email_id FROM email_topics
                 WHERE topic_id IN (SELECT value FROM json_each(?)))"""
    ids = _in(members)
    count, last = conn.execute(
        f"SELECT COUNT(*), MAX(e.date_received) FROM emails e WHERE {tagged} AND {_visible_mail()}",
        (ids,),
    ).fetchone()
    lines: dict[str, list[dict]] = {"threads": [], "decisions": [], "actions": []}
    cur_d, cur_a = _current(conn)
    seen: set[str] = set()
    for email_id, date, subject, cid in conn.execute(
        f"""SELECT e.id, e.date_received, e.subject, e.conversation_id FROM emails e
            WHERE {tagged} AND {_visible_mail()} ORDER BY e.date_received DESC LIMIT 60""",
        (ids,),
    ):
        if cid in seen or len(lines["threads"]) >= _LINES:
            continue
        seen.add(cid)
        lines["threads"].append(_line(f"«{_cut(subject, 120)}»", "email", email_id, date))
    for text, by, email_id, date in conn.execute(
        f"""SELECT d.decision, d.decided_by, d.email_id, e.date_received FROM decisions d
            JOIN emails e ON e.id = d.email_id WHERE {tagged} AND {_visible_mail()} AND {cur_d}
            ORDER BY e.date_received DESC LIMIT ?""",
        (ids, _LINES),
    ):
        who = f" ({by})" if by else ""
        lines["decisions"].append(_line(f"Decided: {_cut(text)}{who}", "email", email_id, date))
    for task, owner, email_id, date in conn.execute(
        f"""SELECT a.task, a.owner, a.email_id, e.date_received FROM action_items a
            JOIN emails e ON e.id = a.email_id WHERE {tagged} AND {_visible_mail()}
              AND COALESCE(a.status, 'open') = 'open' AND {cur_a}
            ORDER BY e.date_received DESC LIMIT ?""",
        (ids, _LINES),
    ):
        who = f" ({owner})" if owner else ""
        lines["actions"].append(_line(f"Open: {_cut(task)}{who}", "email", email_id, date))
    people = [
        name
        for name, _ in conn.execute(
            f"""SELECT p.name, COUNT(*) n FROM email_people ep JOIN people p ON p.id = ep.person_id
                JOIN emails e ON e.id = ep.email_id
                WHERE {tagged} AND {_visible_mail()} AND p.name IS NOT NULL
                GROUP BY p.id ORDER BY n DESC LIMIT 6""",
            (ids,),
        )
    ]
    return {
        "name": named[0],
        "facts": {
            "emails": count,
            "last_at": (last or "")[:10] or None,
            "tags": len(members),
            "people": people,
        },
        "sections": lines,
    }


def _thread_skeleton(conn: sqlite3.Connection, key: str) -> dict | None:
    from src.store import thread_rollups

    rollup = None
    if thread_rollups.enabled(conn):
        held = conn.execute(
            "SELECT emails, last_at FROM thread_rollups WHERE conversation_id = ?", (key,)
        ).fetchone()
        if held is not None:
            rollup = thread_rollups.build(conn, key)
    rollup = rollup or thread_rollups.build(conn, key)
    if rollup is None:
        return None
    newest = conn.execute(
        "SELECT id, date_received FROM emails WHERE conversation_id = ?"
        " ORDER BY date_received DESC, id DESC LIMIT 1",
        (key,),
    ).fetchone()
    lines: dict[str, list[dict]] = {"status": [], "decisions": [], "open": []}
    if rollup.get("summary") and newest:
        lines["status"].append(_line(_cut(rollup["summary"], 400), "email", *newest))
    for d in rollup["decisions"][:_LINES]:
        parent = conn.execute(
            "SELECT d.email_id, e.date_received FROM decisions d JOIN emails e"
            " ON e.id = d.email_id WHERE d.id = ?",
            (d["id"],),
        ).fetchone()
        if parent:
            lines["decisions"].append(_line(f"Decided: {_cut(d['decision'])}", "email", *parent))
    for q in rollup["open_questions"][:_LINES]:
        table = "action_items" if q["kind"] == "action" else "commitments"
        parent = conn.execute(
            f"SELECT t.email_id, e.date_received FROM {table} t JOIN emails e"
            " ON e.id = t.email_id WHERE t.id = ?",
            (q["id"],),
        ).fetchone()
        if parent:
            who = f" ({q['owner']})" if q.get("owner") else ""
            lines["open"].append(_line(f"Open: {_cut(q['text'])}{who}", "email", *parent))
    return {
        "name": rollup["subject"] or key,
        "facts": {
            "emails": rollup["emails"],
            "first_at": (rollup["first_at"] or "")[:10] or None,
            "last_at": (rollup["last_at"] or "")[:10] or None,
            "participants": [p["name"] or p["address"] for p in rollup["participants"][:8]],
        },
        "sections": lines,
    }


def _series_skeleton(conn: sqlite3.Connection, key: str) -> dict | None:
    events = conn.execute(
        """SELECT id, start_at, subject, body_summary, organizer_name FROM calendar_events
           WHERE recurrence_master_id = ? AND is_cancelled = 0 ORDER BY start_at DESC""",
        (key,),
    ).fetchall()
    if not events:
        return None
    now = _now()
    past = [e for e in events if (e[1] or "") < now]
    upcoming = [e for e in events if (e[1] or "") >= now]
    ids = _in(e[0] for e in events)
    lines: dict[str, list[dict]] = {"recent": [], "decisions": [], "actions": []}
    for event_id, start, subject, summary, _ in past[:3]:
        text = _cut(summary) if summary else f"«{_cut(subject, 120)}» held"
        lines["recent"].append(_line(text, "calendar", event_id, start))
    if upcoming:
        nxt = upcoming[-1]
        lines["recent"].insert(0, _line(f"Next: «{_cut(nxt[2], 120)}»", "calendar", nxt[0], nxt[1]))
    cur_d, cur_a = _current(conn)
    for text, event_id, start in conn.execute(
        f"""SELECT d.decision, d.event_id, ce.start_at FROM decisions d
           JOIN calendar_events ce ON ce.id = d.event_id
           WHERE d.event_id IN (SELECT value FROM json_each(?)) AND {cur_d} ORDER BY ce.start_at DESC LIMIT ?""",
        (ids, _LINES),
    ):
        lines["decisions"].append(_line(f"Decided: {_cut(text)}", "calendar", event_id, start))
    for task, event_id, start in conn.execute(
        f"""SELECT a.task, a.event_id, ce.start_at FROM action_items a
           JOIN calendar_events ce ON ce.id = a.event_id
           WHERE a.event_id IN (SELECT value FROM json_each(?))
             AND COALESCE(a.status, 'open') = 'open' AND {cur_a} ORDER BY ce.start_at DESC LIMIT ?""",
        (ids, _LINES),
    ):
        lines["actions"].append(_line(f"Open: {_cut(task)}", "calendar", event_id, start))
    attendance: dict[str, int] = {}
    for (name,) in conn.execute(
        "SELECT COALESCE(name, email) FROM event_attendees"
        " WHERE event_id IN (SELECT value FROM json_each(?))",
        (ids,),
    ):
        attendance[name] = attendance.get(name, 0) + 1
    usual = [n for n, c in sorted(attendance.items(), key=lambda x: -x[1]) if c * 2 >= len(events)]
    return {
        "name": events[0][2] or key,
        "facts": {
            "occurrences": len(events),
            "organizer": events[0][4],
            "last_at": (past[0][1] or "")[:10] if past else None,
            "next_at": (upcoming[-1][1] or "")[:10] if upcoming else None,
            "usual_attendees": usual[:10],
        },
        "sections": lines,
    }


_SKELETONS = {
    "person": _person_skeleton,
    "topic": _topic_skeleton,
    "thread": _thread_skeleton,
    "series": _series_skeleton,
}


def skeleton(conn: sqlite3.Connection, kind: str, key: str) -> dict | None:
    """The deterministic part of one page, or None when the entity holds nothing."""
    skel = _SKELETONS[kind](conn, key)
    if skel is not None:
        skel["sections"] = _dedupe(skel["sections"])
    return skel


def skeleton_refs(skel: dict) -> set[str]:
    return {r for lines in skel["sections"].values() for line in lines for r in line["refs"]}


# --- generations and writes ---------------------------------------------------------------


def _generations(conn: sqlite3.Connection, kind: str) -> tuple[int, int | None]:
    row = conn.execute(
        "SELECT live, building FROM page_generations WHERE kind = ?", (kind,)
    ).fetchone()
    return (row[0], row[1]) if row else (0, None)


def live_generation(conn: sqlite3.Connection, kind: str) -> int:
    return _generations(conn, kind)[0]


def mark_dirty(conn: sqlite3.Connection, kind: str, key: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO page_dirty (kind, entity_key, reason, marked_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(kind, entity_key) DO UPDATE SET reason = excluded.reason,"
        " marked_at = excluded.marked_at",
        (kind, key, reason, _now()),
    )


def compile_page(conn: sqlite3.Connection, kind: str, key: str, *, reason: str) -> bool:
    """Write one entity's skeleton into every generation held (the live one, and the one a
    full build is writing), keeping its synthesis, and mark it dirty when the skeleton
    changed. Whether a page is held for it afterwards."""
    skel = skeleton(conn, kind, key)
    live, building = _generations(conn, kind)
    if skel is None:
        conn.execute("DELETE FROM pages WHERE kind = ? AND entity_key = ?", (kind, key))
        conn.execute("DELETE FROM page_dirty WHERE kind = ? AND entity_key = ?", (kind, key))
        return False
    body = json.dumps(skel, ensure_ascii=False, sort_keys=True)
    changed = False
    for gen in {live, building} - {None}:
        held = conn.execute(
            "SELECT skeleton FROM pages WHERE kind = ? AND entity_key = ? AND generation = ?",
            (kind, key, gen),
        ).fetchone()
        if held is not None and held[0] == body:
            continue
        changed = True
        conn.execute(
            "INSERT INTO pages (kind, entity_key, generation, display_name, skeleton,"
            " skeleton_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(kind, entity_key, generation)"
            " DO UPDATE SET display_name = excluded.display_name, skeleton = excluded.skeleton,"
            " skeleton_at = excluded.skeleton_at",
            (kind, key, gen, skel["name"], body, _now()),
        )
    if changed:
        mark_dirty(conn, kind, key, reason)
    return True


def begin_build(conn: sqlite3.Connection, kind: str) -> int:
    """Start (or resume) a full build of `kind`: a new generation holding the current
    skeletons and no synthesis, invisible until `swap`. Its number."""
    live, building = _generations(conn, kind)
    if building is not None:
        return building
    building = live + 1
    conn.execute(
        "INSERT INTO pages (kind, entity_key, generation, display_name, skeleton, skeleton_at)"
        " SELECT kind, entity_key, ?, display_name, skeleton, skeleton_at FROM pages"
        " WHERE kind = ? AND generation = ?",
        (building, kind, live),
    )
    conn.execute("UPDATE page_generations SET building = ? WHERE kind = ?", (building, kind))
    conn.commit()
    return building


def swap(conn: sqlite3.Connection, kind: str) -> int:
    """Make the generation a full build wrote live, in one transaction, and drop the old
    one. A page the build failed to write keeps the synthesis it had. Its number."""
    live, building = _generations(conn, kind)
    if building is None:
        return live
    conn.execute(
        """UPDATE pages AS n SET synthesis = o.synthesis, evidence_keys = o.evidence_keys,
               built_at = o.built_at, prompt_hash = o.prompt_hash, model = o.model,
               words = o.words, lines_dropped = o.lines_dropped
           FROM pages AS o
           WHERE n.kind = ? AND n.generation = ? AND n.synthesis IS NULL
             AND o.kind = n.kind AND o.entity_key = n.entity_key AND o.generation = ?""",
        (kind, building, live),
    )
    conn.execute("DELETE FROM pages WHERE kind = ? AND generation = ?", (kind, live))
    # A dirty flag older than the synthesis the build wrote is answered by it.
    conn.execute(
        """DELETE FROM page_dirty WHERE kind = ? AND EXISTS (SELECT 1 FROM pages p
               WHERE p.kind = page_dirty.kind AND p.entity_key = page_dirty.entity_key
                 AND p.generation = ? AND p.built_at >= page_dirty.marked_at)""",
        (kind, building),
    )
    conn.execute(
        "UPDATE page_generations SET live = ?, building = NULL, swapped_at = ? WHERE kind = ?",
        (building, _now(), kind),
    )
    conn.commit()
    return building


def write_synthesis(
    conn: sqlite3.Connection,
    kind: str,
    key: str,
    lines: list[dict],
    *,
    generation: int | None = None,
    meta: dict,
) -> None:
    """Store a checked synthesis on one page (the live generation unless named) and clear
    its dirty flag when it went live."""
    gen = live_generation(conn, kind) if generation is None else generation
    words = sum(len(line["text"].split()) for line in lines)
    conn.execute(
        """UPDATE pages SET synthesis = ?, evidence_keys = ?, built_at = ?, prompt_hash = ?,
               model = ?, input_tokens = ?, output_tokens = ?, cost_usd = ?, words = ?,
               lines_dropped = ?
           WHERE kind = ? AND entity_key = ? AND generation = ?""",
        (
            json.dumps(lines, ensure_ascii=False),
            json.dumps(sorted(meta.get("evidence_keys") or [])),
            _now(),
            meta.get("prompt_hash"),
            meta.get("model"),
            meta.get("input_tokens"),
            meta.get("output_tokens"),
            meta.get("cost_usd"),
            words,
            meta.get("lines_dropped"),
            kind,
            key,
            gen,
        ),
    )
    if gen == live_generation(conn, kind):
        conn.execute("DELETE FROM page_dirty WHERE kind = ? AND entity_key = ?", (kind, key))
    conn.commit()


# --- the sync's refresh -------------------------------------------------------------------

# Each source a refresh follows, by the largest id it has seen.
_SOURCES = ("emails", "calendar_events", "decisions", "action_items")


def _watermark(conn: sqlite3.Connection, source: str) -> int | None:
    row = conn.execute("SELECT last_id FROM page_watermarks WHERE source = ?", (source,)).fetchone()
    return row[0] if row else None


def _touched(conn: sqlite3.Connection, marks: dict[str, int]) -> dict[str, set[str]]:
    """The entities the rows past each watermark belong to, by kind."""
    out: dict[str, set[str]] = {kind: set() for kind in KINDS}
    email_ids = {
        r[0] for r in conn.execute("SELECT id FROM emails WHERE id > ?", (marks["emails"],))
    }
    event_ids = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM calendar_events WHERE id > ?", (marks["calendar_events"],)
        )
    }
    for table in ("decisions", "action_items"):
        for email_id, event_id in conn.execute(
            f"SELECT email_id, event_id FROM {table} WHERE id > ?", (marks[table],)
        ):
            if email_id is not None:
                email_ids.add(email_id)
            if event_id is not None:
                event_ids.add(event_id)
    if email_ids:
        ids = _in(email_ids)
        out["person"] |= {
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT person_id FROM email_people"
                " WHERE email_id IN (SELECT value FROM json_each(?))",
                (ids,),
            )
        }
        topic_ids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT topic_id FROM email_topics"
                " WHERE email_id IN (SELECT value FROM json_each(?))",
                (ids,),
            )
        ]
        out["topic"] |= {topic_key(conn, t) for t in topic_ids}
        out["thread"] |= {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT conversation_id FROM emails"
                " WHERE id IN (SELECT value FROM json_each(?)) AND conversation_id IS NOT NULL",
                (ids,),
            )
        }
    if event_ids:
        ids = _in(event_ids)
        out["series"] |= {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT recurrence_master_id FROM calendar_events"
                " WHERE id IN (SELECT value FROM json_each(?)) AND recurrence_master_id IS NOT NULL",
                (ids,),
            )
        }
        out["person"] |= {
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT person_id FROM event_attendees"
                " WHERE event_id IN (SELECT value FROM json_each(?)) AND person_id IS NOT NULL",
                (ids,),
            )
        }
    return out


def refresh(conn: sqlite3.Connection, *, kinds: Iterable[str] = KINDS) -> dict:
    """The sync's Step 8c: compile the skeleton of every eligible entity the rows since the
    last refresh touched, and of every eligible entity on the first refresh. Marks each
    changed page dirty for the synthesis; calls no model. The caller commits.

    Pages of entities no longer eligible stay until a full build retires them.
    """
    if not enabled(conn):
        return {"enabled": False}
    current = {
        s: conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {s}").fetchone()[0] for s in _SOURCES
    }
    held = {s: _watermark(conn, s) for s in _SOURCES}
    first = any(v is None for v in held.values())
    touched = None if first else _touched(conn, {s: v or 0 for s, v in held.items()})
    stats: dict[str, Any] = {"enabled": True, "first": first, "touched": 0, "compiled": 0}
    for kind in kinds:
        keys = eligible(conn, kind)
        if touched is not None:
            wanted = touched[kind]
            keys = [k for k in keys if k in wanted]
        for key in keys:
            stats["touched"] += 1
            if compile_page(conn, kind, key, reason="first compile" if first else "new items"):
                stats["compiled"] += 1
    for source, value in current.items():
        conn.execute(
            "INSERT INTO page_watermarks (source, last_id) VALUES (?, ?)"
            " ON CONFLICT(source) DO UPDATE SET last_id = excluded.last_id",
            (source, value),
        )
    stats["dirty"] = conn.execute("SELECT COUNT(*) FROM page_dirty").fetchone()[0]
    return stats


# --- forgetting ----------------------------------------------------------------------------


def invalidate_refs(conn: sqlite3.Connection, refs: Iterable[str]) -> int:
    """After items are forgotten (issue 174): every page that cited one of `refs` or was
    built from it loses its synthesis, is compiled again from what the store still holds and
    is marked dirty ('forgotten'); a live topic page (topic_pages) built from one is
    deleted. How many pages were touched. The caller commits."""
    wanted = sorted({r for r in refs if r})
    if not wanted:
        return 0
    count = 0
    if _table(conn, "topic_pages"):
        count += conn.execute(
            "DELETE FROM topic_pages WHERE EXISTS (SELECT 1 FROM json_each(topic_pages.evidence_keys)"
            " WHERE value IN (SELECT value FROM json_each(?)))",
            (_in(wanted),),
        ).rowcount
    if not enabled(conn):
        return count
    hits: set[tuple[str, str]] = set()
    for kind, key, skel, evidence in conn.execute(
        "SELECT kind, entity_key, skeleton, evidence_keys FROM pages"
    ):
        cited = skeleton_refs(json.loads(skel)) | set(json.loads(evidence or "[]"))
        if cited & set(wanted):
            hits.add((kind, key))
    for kind, key in sorted(hits):
        conn.execute(
            "UPDATE pages SET synthesis = NULL, evidence_keys = NULL, built_at = NULL"
            " WHERE kind = ? AND entity_key = ?",
            (kind, key),
        )
        compile_page(conn, kind, key, reason="forgotten")
        mark_dirty(conn, kind, key, "forgotten")
    return count + len(hits)


# --- overrides -----------------------------------------------------------------------------


def add_override(
    conn: sqlite3.Connection,
    kind: str,
    key: str,
    action: str,
    *,
    ref: str | None = None,
    text: str | None = None,
) -> int:
    """A correction to a page, applied whenever it is read: 'hide' it, 'drop_ref' (a line
    citing that item is not shown, and the synthesis is not given it), or 'correct' (the
    owner's line, shown first). The page itself is never edited. The new row's id."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if action == "drop_ref" and not ref:
        raise ValueError("drop_ref needs the ref it drops, as kind:id")
    if action == "correct" and not (text or "").strip():
        raise ValueError("correct needs its text")
    cursor = conn.execute(
        "INSERT INTO page_overrides (kind, entity_key, action, ref, text, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (kind, key, action, ref, (text or "").strip() or None, _now()),
    )
    if action != "hide":
        mark_dirty(conn, kind, key, f"override {action}")
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def remove_override(conn: sqlite3.Connection, override_id: int) -> bool:
    cursor = conn.execute("DELETE FROM page_overrides WHERE id = ?", (override_id,))
    conn.commit()
    return cursor.rowcount > 0


def overrides(conn: sqlite3.Connection, kind: str | None = None, key: str | None = None) -> list:
    if not enabled(conn):
        return []
    sql = "SELECT id, kind, entity_key, action, ref, text, created_at FROM page_overrides"
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if key:
        where.append("entity_key = ?")
        args.append(key)
    if where:
        sql += " WHERE " + " AND ".join(where)
    names = ("id", "kind", "key", "action", "ref", "text", "created_at")
    return [dict(zip(names, r, strict=True)) for r in conn.execute(sql + " ORDER BY id", args)]


def dropped_refs(conn: sqlite3.Connection, kind: str, key: str) -> set[str]:
    return {o["ref"] for o in overrides(conn, kind, key) if o["action"] == "drop_ref"}


# --- reading -------------------------------------------------------------------------------

_SECTION_TITLES = {
    "status": "Status",
    "threads": "Threads",
    "decisions": "Decisions",
    "actions": "Open actions",
    "open": "Still owed",
    "meetings": "Meetings",
    "recent": "Recent meetings",
}


def cite(line: dict) -> str:
    """`[kind:id, date]` for every ref of one line: each ref's own date when the line
    carries them (a synthesis line), else the line's."""
    dates = line.get("dates") or {}
    out = []
    for ref in line["refs"]:
        date = dates.get(ref) or line.get("date")
        out.append(f"[{ref}, {date}]" if date else f"[{ref}]")
    return " ".join(out)


def uri(kind: str, key: str) -> str:
    """The page's MCP resource; the key quoted, since an Outlook thread id holds '/'."""
    from urllib.parse import quote

    return f"brain://pages/{kind}/{quote(key, safe='')}"


def _render(kind: str, key: str, row: dict, skel: dict, synthesis, corrections) -> str:
    out = [f"# {skel['name']} ({kind} page)", ""]
    out.append(
        f"Compiled {row['skeleton_at']}"
        + (f"; synthesis {row['built_at']}" if row.get("built_at") else "")
        + ". Derived from the store's items: every line cites [kind:id, date], and"
        " get_item(kind, ref) reads each."
    )
    if corrections:
        out += ["", "## Owner corrections"]
        out += [f"- {c['text']} ({c['created_at'][:10]})" for c in corrections]
    if synthesis:
        out += ["", "## Synthesis"]
        out += [f"- {line['text']} {cite(line)}" for line in synthesis]
    facts = {k: v for k, v in skel["facts"].items() if v not in (None, [], "")}
    if facts:
        out += ["", "## Facts"]
        for name, value in facts.items():
            shown = ", ".join(map(str, value)) if isinstance(value, list) else value
            out.append(f"- {name.replace('_', ' ')}: {shown}")
    for section, lines in skel["sections"].items():
        if lines:
            out += ["", f"## {_SECTION_TITLES.get(section, section.title())}"]
            out += [f"- {line['text']} {cite(line)}" for line in lines]
    return "\n".join(out) + "\n"


def get_page(
    conn: sqlite3.Connection, kind: str, key: str | None, *, generation: int | None = None
) -> dict | None:
    """One live page (or one of `generation`, for a full build) with its overrides applied,
    or None (no step, no page, or hidden)."""
    if key is None or kind not in KINDS or not enabled(conn):
        return None
    gen = live_generation(conn, kind) if generation is None else generation
    cursor = conn.execute(
        "SELECT * FROM pages WHERE kind = ? AND entity_key = ? AND generation = ?",
        (kind, key, gen),
    )
    found = cursor.fetchone()
    if found is None:
        return None
    row = dict(zip([d[0] for d in cursor.description], found, strict=True))
    held = overrides(conn, kind, key)
    if any(o["action"] == "hide" for o in held):
        return None
    dropped = {o["ref"] for o in held if o["action"] == "drop_ref"}
    corrections = [o for o in held if o["action"] == "correct"]

    def keep(lines: list[dict]) -> list[dict]:
        return [line for line in lines if not set(line["refs"]) & dropped]

    skel = json.loads(row["skeleton"])
    skel["sections"] = {s: keep(lines) for s, lines in skel["sections"].items()}
    synthesis = keep(json.loads(row["synthesis"] or "[]"))
    dirty = conn.execute(
        "SELECT reason FROM page_dirty WHERE kind = ? AND entity_key = ?", (kind, key)
    ).fetchone()
    return {
        "kind": kind,
        "key": key,
        "uri": uri(kind, key),
        "name": skel["name"],
        "generation": row["generation"],
        "skeleton_at": row["skeleton_at"],
        "built_at": row["built_at"],
        "derived": True,
        "pending": dirty[0] if dirty else None,
        "facts": skel["facts"],
        "corrections": [{"text": c["text"], "created_at": c["created_at"]} for c in corrections],
        "synthesis": synthesis,
        "lines": [line for lines in skel["sections"].values() for line in lines],
        "markdown": _render(kind, key, row, skel, synthesis, corrections),
    }


def page_for_person(conn: sqlite3.Connection, person_id: int | None) -> dict | None:
    return get_page(conn, "person", None if person_id is None else str(person_id))


def page_for_topic(conn: sqlite3.Connection, topic_id: int | None) -> dict | None:
    if topic_id is None or not enabled(conn):
        return None
    return get_page(conn, "topic", topic_key(conn, topic_id))


def page_for_thread(conn: sqlite3.Connection, conversation_id: str | None) -> dict | None:
    return get_page(conn, "thread", conversation_id)


def page_for_series(conn: sqlite3.Connection, master_id: str | None) -> dict | None:
    return get_page(conn, "series", master_id)


def brief(page: dict | None, lines: int = 3) -> dict | None:
    """A page cut for a dossier or a card: its head, the synthesis (or the first skeleton
    lines when none is written yet), and where the whole page is."""
    if page is None:
        return None
    body = page["synthesis"] or page["lines"][:lines]
    return {
        "kind": page["kind"],
        "key": page["key"],
        "uri": page["uri"],
        "name": page["name"],
        "skeleton_at": page["skeleton_at"],
        "built_at": page["built_at"],
        "pending": page["pending"],
        "derived": True,
        "corrections": page["corrections"],
        "facts": page["facts"],
        "lines": [f"{line['text']} {cite(line)}" for line in body],
    }


def cards(conn: sqlite3.Connection, query: str, limit: int = 3) -> list[dict]:
    """The entity cards a search shows: live pages whose name holds every word of the
    query (case, accent and script blind), the shortest name first."""
    from src.store.greek import search_fold

    if not enabled(conn):
        return []
    words = [w for w in search_fold(query or "").split() if len(w) > 1]
    if not words:
        return []
    found = []
    for kind, key, name in conn.execute(
        "SELECT p.kind, p.entity_key, p.display_name FROM pages p JOIN page_generations g"
        " ON g.kind = p.kind AND g.live = p.generation"
    ):
        folded = search_fold(name or "")
        if all(w in folded.split() or w in folded for w in words):
            found.append((len(folded), KINDS.index(kind), kind, key))
    out = []
    for _, _, kind, key in sorted(found):
        card = brief(get_page(conn, kind, key))
        if card is not None:
            out.append(card)
        if len(out) >= limit:
            break
    return out


def list_pages(conn: sqlite3.Connection, kind: str | None = None, limit: int = 200) -> list:
    if not enabled(conn):
        return []
    sql = (
        "SELECT p.kind, p.entity_key, p.display_name, p.skeleton_at, p.built_at FROM pages p"
        " JOIN page_generations g ON g.kind = p.kind AND g.live = p.generation"
    )
    args: list = []
    if kind:
        sql += " WHERE p.kind = ?"
        args.append(kind)
    sql += " ORDER BY p.kind, p.display_name LIMIT ?"
    args.append(limit)
    return [
        {"kind": k, "key": e, "name": n, "uri": uri(k, e), "skeleton_at": s, "built_at": b}
        for k, e, n, s, b in conn.execute(sql, args)
    ]


def counts(conn: sqlite3.Connection) -> dict:
    if not enabled(conn):
        return {}
    out: dict[str, Any] = {}
    for kind in KINDS:
        live = live_generation(conn, kind)
        total, written = conn.execute(
            "SELECT COUNT(*), COUNT(synthesis) FROM pages WHERE kind = ? AND generation = ?",
            (kind, live),
        ).fetchone()
        dirty = conn.execute("SELECT COUNT(*) FROM page_dirty WHERE kind = ?", (kind,)).fetchone()
        out[kind] = {"pages": total, "synthesised": written, "dirty": dirty[0], "generation": live}
    return out

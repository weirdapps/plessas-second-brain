"""Live topic pages: a cited account of each topic the owner keeps returning to.

The owner comes back to a handful of live topics: in six weeks one budget topic came up
in nine sessions over eight days, and a status question is the costliest kind he asks
(a median of 18 brain calls, up to 49). Agents dug up earlier sessions 56 times, and he
once had to find his own reconciliation from four days before again. A page per live
topic holds where it stands, so the next session starts there.

What the field learned about such pages (July-October 2026) sets the rules:
- few pages: at most PAGE_CAP, chosen by a deterministic rule (select_live_topics);
- every line cites the store items it rests on as `kind:ref`, and a post-check drops
  any line whose refs were not in the evidence the model was given;
- a page is regenerated from the sources, never edited, and only when new evidence came
  in (or the prompt changed);
- a page is never a source: it is kept in `topic_pages` (schema v36), out of every
  full-text index and the vector index, and no page or finding is built from another
  page. Findings (src/store/findings.py) appear on a page, below the primary items.

This module picks the topics, gathers what a page may cite and reads the pages held;
src/pipeline/topic_pages.py is the job that writes them, one model call a page.
"""

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.store.findings import findings_matching
from src.store.greek import register_sql_functions, search_fold
from src.store.normalizer import normalize_topic
from src.store.owner import is_owner_address, owner_address_sql

# ----------------------------------------------------------------- selection

# The window a topic must be live in.
WINDOW_DAYS = 30
# The owner sessions a topic must come up in to be picked for its sessions: sessions in
# which he asked the brain something. Most of his sessions are coding ones, whose topics
# (the tools, the hosts) say nothing about his work.
SESSION_MIN = 3
# How a session's turn records a call to the brain's MCP tools (conversation_export.py
# writes "[Tool: <name>]"; the server is registered as `second-brain`).
BRAIN_TOOL_MARK = "[Tool: mcp__second-brain__"
# The owner's discussion threads a session topic must also have: a page needs primary
# evidence, and a topic the owner only codes about has none.
SESSION_THREADS_MIN = 2
# The owner's discussion threads a topic needs to be picked for its mail alone.
MAIL_THREADS_MIN = 3
# A mail topic's share of its year that fell in the window, against the store's own share:
# at least twice, so a topic always present (the employer's name, a unit's name, meeting
# logistics) is not live.
LIFT_MIN = 2.0
# No lift test while the store holds less mail before the window than in it.
YOUNG_STORE_SHARE = 0.5
PAGE_CAP = 20
# The topic rows a named page (BRAIN_TOPIC_PAGES) covers at most.
NAMED_MEMBERS_MAX = 12
# A reply in a calendar thread is not a discussion (src/store/query.py has the same list).
_MEETING_RESPONSES = ("accepted:", "tentative:", "declined:", "αποδεκτη:", "αποδοχη:")
# A page whose topic is no longer picked is removed once it is this old.
RETIRE_AFTER_DAYS = 14


@dataclass
class LiveTopic:
    """A page's topic: its name, the topic rows it covers, and why it was picked."""

    name: str
    members: list[tuple[int, str]]
    selected_by: str  # named | sessions | mail
    sessions: int = 0
    threads: int = 0
    words: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return normalize_topic(self.name)

    @property
    def member_ids(self) -> list[int]:
        return [topic_id for topic_id, _ in self.members]


def _words(name: str) -> frozenset[str]:
    """A topic name's words, folded, a Latin plural 's' taken off, for family merges."""
    out = set()
    for word in search_fold(normalize_topic(name)).split():
        if len(word) > 3 and word.isascii() and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if any(c.isalnum() for c in word):
            out.add(word)
    return frozenset(out)


def named_topics(value: str | None = None) -> list[list[str]]:
    """BRAIN_TOPIC_PAGES as a list of pages, each the phrases it covers.

    Pages are separated by ';', the phrases of one page by '|', the first naming the page:
    "budget 2027|business plan 2027; data platform". Topic tags fragment ("2027 budget",
    "exco budget 2027"), so a page covers every topic row holding all the words of any of
    its phrases.
    """
    raw = os.environ.get("BRAIN_TOPIC_PAGES", "") if value is None else value
    pages = []
    for entry in raw.split(";"):
        phrases = [p.strip() for p in entry.split("|") if normalize_topic(p.strip())]
        if phrases:
            pages.append(phrases)
    return pages


def skip_topics(value: str | None = None) -> list[str]:
    """BRAIN_TOPIC_PAGES_SKIP: topic tags, separated by ';', that never get a page of their
    own or join another's (a unit's name the rule finds live in a busy month)."""
    raw = os.environ.get("BRAIN_TOPIC_PAGES_SKIP", "") if value is None else value
    return [name.strip() for name in raw.split(";") if normalize_topic(name.strip())]


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _in(values: Iterable) -> str:
    return json.dumps(list(values))


def _like(text: str) -> str:
    """`text` as a LIKE pattern matching it anywhere, its wildcards escaped with '\\'."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _owner_sessions(conn: sqlite3.Connection, since: str) -> dict[int, set[int]]:
    """topic id -> the owner's sessions that carried it since `since` and called the brain:
    sessions holding a turn the owner typed, so a teammate's or a subagent's never counts,
    and a call to one of the brain's tools."""
    found: dict[int, set[int]] = {}
    for topic_id, conversation_id in conn.execute(
        """SELECT ct.topic_id, ct.conversation_id FROM conversation_topics ct
           JOIN conversations c ON c.id = ct.conversation_id
           WHERE c.started_at >= ?
             AND EXISTS (SELECT 1 FROM conversation_turns k
                         WHERE k.conversation_id = c.id AND +k.speaker = 'user')
             AND EXISTS (SELECT 1 FROM conversation_turns k
                         WHERE k.conversation_id = c.id AND k.content LIKE ? ESCAPE '\\')""",
        (since, _like(BRAIN_TOOL_MARK)),
    ):
        found.setdefault(topic_id, set()).add(conversation_id)
    return found


def _owner_threads(conn: sqlite3.Connection, since: str, pattern: str) -> dict[int, set[str]]:
    """topic id -> the owner's discussion threads since `since`.

    A discussion thread holds two or more mail messages in the window (a broadcast is one
    message a thread), at least one sent by the owner or addressed to him; mail only, no
    news, automation, documents or session notes, and no meeting responses.
    """
    rows = conn.execute(
        """SELECT id, conversation_id, sender_address, subject FROM emails
           WHERE date_received >= ? AND source_class = 'mail' AND conversation_id IS NOT NULL""",
        (since,),
    ).fetchall()
    rows = [r for r in rows if not search_fold(r[3] or "").startswith(_MEETING_RESPONSES)]
    by_thread: dict[str, list[int]] = {}
    for email_id, thread, _, _ in rows:
        by_thread.setdefault(thread, []).append(email_id)
    sent = {r[0] for r in rows if is_owner_address(r[2], pattern)}
    condition, params = owner_address_sql("p.email", pattern)
    owner_ids = [r[0] for r in conn.execute(f"SELECT id FROM people p WHERE {condition}", params)]
    addressed: set[int] = set()
    if owner_ids:
        addressed = {
            r[0]
            for r in conn.execute(
                """SELECT ep.email_id FROM email_people ep JOIN emails e ON e.id = ep.email_id
                   WHERE ep.person_id IN (SELECT value FROM json_each(?))
                     AND ep.role_in_email = 'recipient' AND e.date_received >= ?""",
                (_in(owner_ids), since),
            )
        }
    involved = {
        thread: ids
        for thread, ids in by_thread.items()
        if len(ids) >= 2 and any(i in sent or i in addressed for i in ids)
    }
    thread_of = {i: thread for thread, ids in involved.items() for i in ids}
    found: dict[int, set[str]] = {}
    if thread_of:
        for email_id, topic_id in conn.execute(
            "SELECT email_id, topic_id FROM email_topics"
            " WHERE email_id IN (SELECT value FROM json_each(?))",
            (_in(thread_of),),
        ):
            found.setdefault(topic_id, set()).add(thread_of[email_id])
    return found


def _lifts(
    conn: sqlite3.Connection, topic_ids: list[int], since: str, year: str
) -> dict[int, float | None]:
    """topic id -> its mail share of the year that fell in the window, over the store's;
    None for every topic while the store is too young to have a year to compare with."""
    total, recent = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(date_received >= ?), 0) FROM emails"
        " WHERE source_class = 'mail' AND date_received >= ?",
        (since, year),
    ).fetchone()
    if not total or recent / total > YOUNG_STORE_SHARE:
        return dict.fromkeys(topic_ids)
    base = recent / total
    lifts: dict[int, float | None] = dict.fromkeys(topic_ids, 0.0)
    for topic_id, year_count, window_count in conn.execute(
        """SELECT et.topic_id, COUNT(*), COALESCE(SUM(e.date_received >= ?), 0)
           FROM email_topics et JOIN emails e ON e.id = et.email_id
           WHERE et.topic_id IN (SELECT value FROM json_each(?))
             AND e.source_class = 'mail' AND e.date_received >= ?
           GROUP BY et.topic_id""",
        (since, _in(topic_ids), year),
    ):
        lifts[topic_id] = (window_count / year_count) / base if year_count else 0.0
    return lifts


def _topic_names(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, str]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT id, name FROM topics WHERE id IN (SELECT value FROM json_each(?))",
            (_in(ids),),
        )
    }


def _named_members(
    conn: sqlite3.Connection, phrases: list[str], since: str
) -> list[tuple[int, str]]:
    """The topic rows holding every word of one of `phrases`, the most used since
    `since` first, at most NAMED_MEMBERS_MAX of those used at all in that time."""
    wanted = [_words(p) for p in phrases]
    matched = {
        topic_id: name
        for topic_id, name in conn.execute("SELECT id, name FROM topics")
        if any(w and w <= _words(name) for w in wanted)
    }
    if not matched:
        return []
    uses = dict(
        conn.execute(
            """SELECT et.topic_id, COUNT(*) FROM email_topics et
               JOIN emails e ON e.id = et.email_id
               WHERE et.topic_id IN (SELECT value FROM json_each(?)) AND e.date_received >= ?
               GROUP BY et.topic_id""",
            (_in(matched), since),
        ).fetchall()
    )
    for topic_id, n in conn.execute(
        """SELECT ct.topic_id, COUNT(*) FROM conversation_topics ct
           JOIN conversations c ON c.id = ct.conversation_id
           WHERE ct.topic_id IN (SELECT value FROM json_each(?)) AND c.started_at >= ?
           GROUP BY ct.topic_id""",
        (_in(matched), since),
    ):
        uses[topic_id] = uses.get(topic_id, 0) + n
    used = sorted((t for t in matched if uses.get(t)), key=lambda t: (-uses[t], t))
    return [(t, matched[t]) for t in used[:NAMED_MEMBERS_MAX]]


def select_live_topics(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    named: list[list[str]] | None = None,
    cap: int = PAGE_CAP,
    owner_pattern: str | None = None,
) -> list[LiveTopic]:
    """The topics that get a page, at most `cap`, in this order:

    1. every page BRAIN_TOPIC_PAGES names (`named`), whether live or not;
    2. topics the owner's own sessions came back to: SESSION_MIN or more sessions in the
       last WINDOW_DAYS in which he called the brain, with SESSION_THREADS_MIN or more of
       his discussion threads, the most sessions first;
    3. topics with the most of his discussion threads, MAIL_THREADS_MIN or more.

    Both 2 and 3 must be live: the topic's share of the year's mail that fell in the window
    is at least LIFT_MIN times the store's own share, so a topic as present all year (the
    employer, a unit's name, meeting logistics) is not picked.

    Tags that hold another picked tag's words ("okapi pilot results" and "okapi pilot") make one page,
    named by the shorter, covering both.
    """
    from src import config

    now = now or datetime.now(UTC)
    pattern = config.USER_EMAIL_PATTERN if owner_pattern is None else owner_pattern
    named = named_topics() if named is None else named
    since = _cutoff(now, WINDOW_DAYS)
    year = _cutoff(now, 365)
    evidence_since = _cutoff(now, EVIDENCE_DAYS)

    pages: list[LiveTopic] = []
    for phrases in named:
        members = _named_members(conn, phrases, evidence_since)
        pages.append(LiveTopic(phrases[0], members, "named", words=sorted(_words(phrases[0]))))

    sessions = _owner_sessions(conn, since)
    threads = _owner_threads(conn, since, pattern)
    candidates = [t for t, s in threads.items() if len(s) >= SESSION_THREADS_MIN]
    lifts = _lifts(conn, candidates, since, year)
    live = {t for t in candidates if lifts[t] is None or (lifts[t] or 0) >= LIFT_MIN}
    session_topics = [t for t, s in sessions.items() if len(s) >= SESSION_MIN and t in live]
    mail_topics = [
        t for t in live if len(threads[t]) >= MAIL_THREADS_MIN and t not in session_topics
    ]
    names = _topic_names(conn, session_topics + mail_topics)
    taken = {t for page in pages for t in page.member_ids}
    skipped = {normalize_topic(name) for name in skip_topics()}
    taken |= {t for t, name in names.items() if normalize_topic(name) in skipped}

    # Families: a picked tag joins the first picked tag whose words it holds, shortest first.
    families: list[dict] = []
    for topic_id in sorted(
        (t for t in session_topics + mail_topics if t in names and t not in taken),
        key=lambda t: (len(_words(names[t])), names[t]),
    ):
        words = _words(names[topic_id])
        if not words:
            continue
        home = next((fam for fam in families if fam["words"] <= words), None)
        if home is None:
            home = {"words": words, "head": topic_id, "members": [], "sessions": set()}
            home["threads"] = set()
            families.append(home)
        home["members"].append(topic_id)
        home["sessions"] |= sessions.get(topic_id, set())
        home["threads"] |= threads.get(topic_id, set())
    for fam in families:
        by_sessions = any(m in session_topics for m in fam["members"])
        fam["selected_by"] = "sessions" if by_sessions else "mail"
    families.sort(
        key=lambda fam: (
            fam["selected_by"] != "sessions",
            -len(fam["sessions"]) if fam["selected_by"] == "sessions" else 0,
            -len(fam["threads"]),
            names[fam["head"]],
        )
    )
    for fam in families:
        if len(pages) >= cap:
            break
        pages.append(
            LiveTopic(
                names[fam["head"]],
                [(m, names[m]) for m in fam["members"]],
                fam["selected_by"],
                sessions=len(fam["sessions"]),
                threads=len(fam["threads"]),
                words=sorted(fam["words"]),
            )
        )
    return pages


# ------------------------------------------------------------------ evidence

# How far back a page's evidence goes, and how many rows of each list it takes.
EVIDENCE_DAYS = 120
_PER_MEMBER = 15
_CAPS = {
    "decision": 25,
    "fact": 25,
    "action": 12,
    "email": 15,
    "attachment": 10,
    "figure": 10,
    "finding": 15,
    "session": 6,
    "deliverable": 8,
}
_TEXT_CHARS = 400
_SUMMARY_CHARS = 700
# Kinds whose rows rest on the owner's sessions or notes, not on primary items.
DERIVED_KINDS = ("finding", "conversation")


@dataclass
class Evidence:
    """What one page is written from: rows by section, each with its cite key."""

    rows: dict[str, list[dict]] = field(default_factory=dict)

    def add(self, section: str, key: str, date: str | None, text: str, **extra) -> None:
        bucket = self.rows.setdefault(section, [])
        if len(bucket) < _CAPS[section] and text:
            bucket.append({"key": key, "date": (date or "")[:10], "text": text, **extra})

    @property
    def keys(self) -> set[str]:
        return {row["key"] for rows in self.rows.values() for row in rows}

    @property
    def count(self) -> int:
        return sum(len(rows) for rows in self.rows.values())

    @property
    def newest(self) -> str | None:
        dates = [r["date"] for rows in self.rows.values() for r in rows if r["date"]]
        return max(dates) if dates else None

    def label(self, key: str) -> dict:
        for section, rows in self.rows.items():
            for row in rows:
                if row["key"] == key:
                    return {
                        "ref": key,
                        "kind": key.split(":", 1)[0],
                        "date": row["date"] or None,
                        "title": row.get("title") or row["text"][:120],
                        "section": section,
                    }
        return {"ref": key, "kind": key.split(":", 1)[0], "date": None, "title": None}


def _cut(text: str | None, chars: int = _TEXT_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= chars else text[: chars - 1] + "…"


def _key(row: dict) -> str | None:
    kind, ref = row.get("kind"), row.get("ref")
    return f"{kind}:{ref}" if kind and ref is not None else None


def _newest_first(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r.get("date") or "", reverse=True)


def _context_rows(conn: sqlite3.Connection, live: LiveTopic) -> dict[str, list[dict]]:
    """topic_context's rows for every member, merged; session notes left out (derived)."""
    from src.store.context import get_topic_context

    merged: dict[str, list[dict]] = {"decisions": [], "key_facts": [], "open_actions": []}
    merged["recent_emails"] = []
    seen: set[tuple] = set()
    for _, name in live.members:
        out = get_topic_context(conn, name, days=EVIDENCE_DAYS, limit=_PER_MEMBER)
        if not out.get("topic") or out["topic"]["name"] != name:
            continue
        for section in merged:
            for row in out.get(section) or []:
                if row.get("source_class") == "session_note":
                    continue
                text = row.get("decision") or row.get("fact") or row.get("task") or ""
                ident = (section, _key(row), text or row.get("subject"))
                if ident not in seen:
                    seen.add(ident)
                    merged[section].append(row)
    return {section: _newest_first(rows) for section, rows in merged.items()}


def _attachments(conn: sqlite3.Connection, ids: list[int], since: str) -> list:
    return conn.execute(
        """SELECT a.id, a.filename, e.date_received, e.sender_address, ac.summary
           FROM attachments a
           JOIN emails e ON e.id = a.email_id
           JOIN attachment_content ac ON ac.attachment_id = a.id
           WHERE a.email_id IN (SELECT email_id FROM email_topics
                                WHERE topic_id IN (SELECT value FROM json_each(?)))
             AND e.date_received >= ? AND COALESCE(a.is_inline, 0) = 0
             AND ac.summary IS NOT NULL AND e.source_class IN ('mail', 'document')
           ORDER BY e.date_received DESC LIMIT ?""",
        (_in(ids), since, _CAPS["attachment"]),
    ).fetchall()


def _figures(conn: sqlite3.Connection, query: str) -> list[dict]:
    """The figures registry's latest values for the topic, when the registry exists."""
    import importlib

    # Looked up, not imported: the registry is another change's, and may not be here yet.
    try:
        search_figures = importlib.import_module("src.store.figures").search_figures
    except (ImportError, AttributeError):
        return []
    try:
        out = search_figures(conn, query, limit=_CAPS["figure"])
    except Exception:  # a registry that fails must not cost the page
        return []
    return list((out or {}).get("result") or [])


def _sessions(conn: sqlite3.Connection, ids: list[int], since: str) -> list:
    return conn.execute(
        """SELECT c.session_id, c.started_at, c.summary FROM conversations c
           WHERE c.id IN (SELECT conversation_id FROM conversation_topics
                          WHERE topic_id IN (SELECT value FROM json_each(?)))
             AND c.started_at >= ? AND c.summary IS NOT NULL
             AND EXISTS (SELECT 1 FROM conversation_turns k
                         WHERE k.conversation_id = c.id AND +k.speaker = 'user')
           ORDER BY c.started_at DESC LIMIT ?""",
        (_in(ids), since, _CAPS["session"]),
    ).fetchall()


def _notes(conn: sqlite3.Connection, ids: list[int], words: list[str], since: str) -> list:
    """Session notes about the topic: tagged with a member, or naming its words in their
    path. Only their path and date are evidence: what a note says is derived."""
    tagged = {
        r[0]
        for r in conn.execute(
            "SELECT email_id FROM email_topics WHERE topic_id IN (SELECT value FROM json_each(?))",
            (_in(ids),),
        )
    }
    found = []
    for email_id, subject, date in conn.execute(
        """SELECT id, subject, date_received FROM emails
           WHERE source_class = 'session_note' AND date_received >= ?
           ORDER BY date_received DESC""",
        (since,),
    ):
        folded = search_fold(subject or "")
        if email_id in tagged or (words and all(w in folded for w in words)):
            found.append((email_id, subject, date))
    return found[: _CAPS["deliverable"]]


def gather_evidence(
    conn: sqlite3.Connection,
    live: LiveTopic,
    *,
    now: datetime | None = None,
    owner_pattern: str | None = None,
) -> Evidence:
    """Everything one page may cite, newest first in each section.

    Primary: topic_context's decisions, key facts, open actions and emails for every topic
    row the page covers, the newest attachment summaries, and the figures registry's latest
    values when it exists. Derived, and marked so: findings, the owner's sessions on the
    topic, and the session notes about it, which count as deliverables only. Never a topic
    page: no page is evidence for another.
    """
    from src import config

    now = now or datetime.now(UTC)
    pattern = config.USER_EMAIL_PATTERN if owner_pattern is None else owner_pattern
    since = _cutoff(now, EVIDENCE_DAYS)
    ids = live.member_ids
    register_sql_functions(conn)
    evidence = Evidence()
    if not ids and not live.words:
        return evidence
    rows = _context_rows(conn, live) if ids else {}
    for row in rows.get("decisions", []):
        by = f" (decided by {row['decided_by']})" if row.get("decided_by") else ""
        evidence.add("decision", _key(row) or "", row.get("date"), _cut(row["decision"]) + by)
    for row in rows.get("key_facts", []):
        evidence.add("fact", _key(row) or "", row.get("date"), _cut(row["fact"]))
    for row in rows.get("open_actions", []):
        owner = f"; owner {row['owner']}" if row.get("owner") else ""
        due = f"; due {row['deadline']}" if row.get("deadline") else ""
        evidence.add("action", _key(row) or "", row.get("date"), _cut(row["task"]) + owner + due)
    for row in rows.get("recent_emails", []):
        text = f"{_cut(row.get('subject'), 160)}: {_cut(row.get('summary'))}"
        evidence.add(
            "email", _key(row) or "", row.get("date"), text, title=_cut(row.get("subject"), 120)
        )
    if ids:
        for att_id, filename, date, sender, summary in _attachments(conn, ids, since):
            sent = " (sent by the owner)" if is_owner_address(sender, pattern) else ""
            evidence.add(
                "attachment",
                f"attachment:{att_id}",
                date,
                f"{filename}{sent}: {_cut(summary, _SUMMARY_CHARS)}",
                title=filename,
            )
    for fig in _figures(conn, live.name):
        latest = fig.get("latest") or {}
        source = latest.get("source")
        if not source:
            continue
        unit = fig.get("unit") or ""
        prior = fig.get("previous") or {}
        before = f"; before: {prior.get('value')} for {prior.get('period')}" if prior else ""
        evidence.add(
            "figure",
            source,
            latest.get("edition_date") or latest.get("period_end"),
            f"{fig.get('metric')}: {latest.get('value')}{unit} for {latest.get('period')}"
            f" (sheet {fig.get('sheet')}, cell {latest.get('cell')}){before}",
            title=fig.get("metric"),
        )
    # Accepted findings only (src/store/findings.py: the source is held and the quote or
    # value is in its text), the owner's own sessions' first, then agents'.
    for finding in findings_matching(conn, topic_ids=ids, words=live.words, limit=_CAPS["finding"]):
        by = "the owner's session" if finding["author_kind"] == "owner" else "an agent session"
        superseded = (
            f"; SUPERSEDED by {finding['superseded_by']}" if finding["superseded_by"] else ""
        )
        value = f" = {finding['value']}" if finding["value"] else ""
        as_of = f" as of {finding['as_of']}" if finding["as_of"] else ""
        evidence.add(
            "finding",
            finding["ref"],
            finding["date"],
            f"{_cut(finding['claim'])}{value}{as_of} (filed by {by}; rests on "
            f"{finding['source_ref']}{superseded})",
            title=finding["claim"],
        )
    if ids:
        for session_id, started, summary in _sessions(conn, ids, since):
            evidence.add("session", f"conversation:{session_id}", started, _cut(summary))
    for email_id, subject, date in _notes(conn, ids, live.words, since):
        evidence.add(
            "deliverable",
            f"document:{email_id}",
            date,
            f"note written by a session: {_cut(subject, 200)}",
            title=subject,
        )
    return evidence


# ----------------------------------------------------------------- the store


def held_page(conn: sqlite3.Connection, key: str) -> dict | None:
    try:
        cursor = conn.execute("SELECT * FROM topic_pages WHERE topic = ?", (key,))
    except sqlite3.OperationalError:  # a store older than v36
        return None
    row = cursor.fetchone()
    if row is None:
        return None
    names = [d[0] for d in cursor.description]
    return dict(zip(names, row, strict=True))


def retire_pages(
    conn: sqlite3.Connection, keep: Iterable[str], *, now: datetime | None = None
) -> int:
    """Remove the pages of topics no longer picked once they are RETIRE_AFTER_DAYS old, so
    the set stays small. Returns how many went."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=RETIRE_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    keys = list(keep)
    cursor = conn.execute(
        "DELETE FROM topic_pages WHERE built_at < ?"
        " AND topic NOT IN (SELECT value FROM json_each(?))",
        (cutoff, _in(keys)),
    )
    conn.commit()
    return cursor.rowcount


# ------------------------------------------------------------------ reading


def _as_page(row: dict) -> dict:
    citations = json.loads(row["citations"] or "[]")
    return {
        "topic": row["display_name"],
        "key": row["topic"],
        "selected_by": row["selected_by"],
        "members": [m["name"] for m in json.loads(row["members"] or "[]")],
        "built_at": row["built_at"],
        "newest_evidence_at": row["newest_evidence_at"],
        "body": row["body"],
        "citations": citations,
        "derived_citations": sum(c.get("kind") in DERIVED_KINDS for c in citations),
        "lines_dropped": row["lines_dropped"],
    }


def list_pages(conn: sqlite3.Connection) -> list[dict]:
    """Every page held: its topic, when it was built and what it covers."""
    try:
        cursor = conn.execute(
            "SELECT display_name, topic, built_at, members FROM topic_pages ORDER BY display_name"
        )
    except sqlite3.OperationalError:
        return []
    return [
        {
            "topic": name,
            "key": key,
            "built_at": built,
            "members": [m["name"] for m in json.loads(members or "[]")],
        }
        for name, key, built, members in cursor.fetchall()
    ]


def find_page(
    conn: sqlite3.Connection, topic: str, topic_id: int | None = None, *, fuzzy: bool = True
) -> dict | None:
    """The page about `topic`: its own name, or a tag it covers (or the tag `topic_id`);
    with `fuzzy`, else the page whose name holds all of the query's words or whose words
    the query holds, the most specific first. None when no page fits."""
    key = normalize_topic(topic or "")
    try:
        rows = conn.execute("SELECT * FROM topic_pages").fetchall()
        names = [d[0] for d in conn.execute("SELECT * FROM topic_pages LIMIT 0").description]
    except sqlite3.OperationalError:
        return None
    pages = [dict(zip(names, r, strict=True)) for r in rows]
    if not pages or not (key or topic_id):
        return None
    for page in pages:
        members = json.loads(page["members"] or "[]")
        if page["topic"] == key or any(
            normalize_topic(m["name"]) == key or (topic_id is not None and m["id"] == topic_id)
            for m in members
        ):
            return _as_page(page)
    query = _words(key)
    if not fuzzy or not query:
        return None
    fits = [p for p in pages if query <= _words(p["topic"]) or _words(p["topic"]) <= query]
    fits.sort(key=lambda p: (-len(_words(p["topic"]) & query), len(_words(p["topic"]))))
    return _as_page(fits[0]) if fits else None

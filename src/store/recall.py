"""Unified recall — single 'ask once, get everything' entry point.

Composes keyword search across every text-bearing index in the brain
(emails+attachments+standalone docs, conversations, decisions, actions,
inline images) and auto-pulls person/topic context when the query matches
a known person name/email or topic. Used by both the /recall skill and
by other agents through the MCP `recall` tool.
"""

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Hashable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.store import envelope, ranking
from src.store.consolidate import recall_filters
from src.store.context import get_person_context, get_topic_context
from src.store.conversation_query import search_conversations_keyword
from src.store.cross_script import name_forms
from src.store.envelope import EMAIL_KINDS, classes_for
from src.store.fusion import reciprocal_rank_fusion
from src.store.greek import (
    PHRASE_MATCH,
    register_sql_functions,
    sb_match_args,
    search_fold,
)
from src.store.normalizer import normalize_topic
from src.store.ordering import action_order, decision_dates, decision_order, parent_dates
from src.store.person_resolve import IDENTIFIER_KINDS, resolve_identity
from src.store.query import (
    fts5_query_variants,
    query_by_keyword,
    search_attachments,
    thread_keys,
)
from src.store.source_class import (
    MISSING,
    column_missing,
    visible_ids,
    visible_sql,
    with_source_class,
)
from src.store.sql_readonly import connect_read_only
from src.store.teams_query import search_teams as _search_teams_q
from src.store.whatsapp_query import search_whatsapp as _search_whatsapp_q

logger = logging.getLogger(__name__)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _folded_bucket(conn: sqlite3.Connection, sql: str, keyword: str, limit: int) -> list[dict]:
    """Run a LIKE-style bucket in one pass: the whole query, else any of its tokens.

    ``sql`` takes (phrase, words, tokens, limit), selects sb_match(...) AS score
    and orders by it first. Matching is on folded text (case, accents, final
    sigma), at the start of a word, which LIKE alone does not do. A row holding
    the phrase, or every word of the query but the stopwords in any order (years
    and short words included, as in the full-text buckets), is a whole match, and
    whole matches come back alone when there are any. Otherwise rows holding
    some of its tokens come back, most first, flagged partial_match: the
    whole-query form alone emptied these buckets for the long queries agents
    write. A one-word query gets no token pass, since "any" would equal "all".
    A Greek word is looked for by its stem where it has one, as the full-text
    buckets look for it (greek.sb_match_args).
    """
    register_sql_functions(conn)  # the MCP image search calls this directly
    phrase, words, tokens = sb_match_args(keyword)
    if not phrase:
        return []
    rows = [dict(r) for r in conn.execute(sql, (phrase, words, tokens, limit))]
    whole = [r for r in rows if r["score"] >= PHRASE_MATCH]
    out = whole or [{**r, "partial_match": True} for r in rows]
    for row in out:
        del row["score"]
    return out


# Each bucket scores every row once, in a MATERIALIZED CTE. Selected from a plain
# subquery, SQLite flattened it and ran sb_match again for the sort key of every
# matching row, and the score is a Python call.


def _search_decisions(
    conn: sqlite3.Connection, keyword: str, limit: int, visible: str = "1"
) -> list[dict]:
    # Dated, titled and sourced like query_decisions: a decision without its own
    # date takes its parent's, so a meeting's decisions no longer sink below every
    # dated one, and the row says which meeting or thread it came from. `visible`
    # (source_class.visible_sql on d.email_id) leaves out news and automation.
    rows = _folded_bucket(
        conn,
        f"""
        WITH scored AS MATERIALIZED (
            SELECT id, sb_match(decision, ?, ?, ?) AS score FROM decisions
        )
        SELECT d.id, d.email_id, d.event_id, d.teams_thread_id, d.whatsapp_thread_id,
               d.decision, d.decided_by, d.decision_date, {decision_dates()},
               COALESCE(e.subject, tt.title, wt.title, ce.subject, c.summary) AS email_subject,
               CASE
                   WHEN e.id IS NOT NULL THEN 'email'
                   WHEN tt.id IS NOT NULL THEN 'teams'
                   WHEN wt.id IS NOT NULL THEN 'whatsapp'
                   WHEN ce.id IS NOT NULL THEN 'calendar'
                   WHEN c.id IS NOT NULL THEN 'conversation'
                   ELSE 'orphan'
               END AS source,
               s.score
        FROM scored s
        JOIN decisions d ON d.id = s.id
        LEFT JOIN emails e ON e.id = d.email_id
        LEFT JOIN teams_threads tt ON tt.id = d.teams_thread_id
        LEFT JOIN whatsapp_threads wt ON wt.id = d.whatsapp_thread_id
        LEFT JOIN calendar_events ce ON ce.id = d.event_id
        LEFT JOIN conversation_turns ct ON ct.id = d.conversation_turn_id
        LEFT JOIN conversations c ON c.id = ct.conversation_id
        WHERE s.score > 0 AND {visible}
        ORDER BY s.score DESC, {decision_order()}
        LIMIT ?
        """,
        keyword,
        limit,
    )
    return envelope.extracted(conn, with_source_class(conn, rows), "decisions", "id")


def _search_actions(
    conn: sqlite3.Connection, keyword: str, limit: int, visible: str = "1"
) -> list[dict]:
    # Outstanding first, as query_action_items sorts: open before anything else,
    # then upcoming dates soonest first, then undated or free text, then overdue,
    # most recently missed first. Every whole match scores the same, so this
    # decides the page, and ascending deadline put the oldest dates first: items
    # long since expired, with a free-text '2026' ahead of every real date. Every
    # parent is joined, as for decisions, so a Teams, meeting or conversation
    # action is dated, titled and sourced too.
    rows = _folded_bucket(
        conn,
        f"""
        WITH scored AS MATERIALIZED (
            SELECT id, sb_match(task, ?, ?, ?) AS score FROM action_items
        )
        SELECT a.id, a.email_id, a.task, a.owner, a.deadline, a.status,
               COALESCE(e.subject, tt.title, wt.title, ce.subject, c.summary) AS email_subject,
               {parent_dates()},
               CASE
                   WHEN e.id IS NOT NULL THEN 'email'
                   WHEN tt.id IS NOT NULL THEN 'teams'
                   WHEN wt.id IS NOT NULL THEN 'whatsapp'
                   WHEN ce.id IS NOT NULL THEN 'calendar'
                   WHEN c.id IS NOT NULL THEN 'conversation'
                   ELSE 'orphan'
               END AS source,
               s.score
        FROM scored s
        JOIN action_items a ON a.id = s.id
        LEFT JOIN emails e ON e.id = a.email_id
        LEFT JOIN teams_threads tt ON tt.id = a.teams_thread_id
        LEFT JOIN whatsapp_threads wt ON wt.id = a.whatsapp_thread_id
        LEFT JOIN calendar_events ce ON ce.id = a.event_id
        LEFT JOIN conversation_turns ct ON ct.id = a.conversation_turn_id
        LEFT JOIN conversations c ON c.id = ct.conversation_id
        WHERE s.score > 0 AND {visible}
        ORDER BY s.score DESC, a.status IS NOT 'open', {action_order()}
        LIMIT ?
        """,
        keyword,
        limit,
    )
    return envelope.extracted(conn, with_source_class(conn, rows), "action_items", "id")


def _search_commitments(
    conn: sqlite3.Connection, keyword: str, limit: int, visible: str = "1"
) -> list[dict]:
    if not _table_exists(conn, "commitments"):
        return []
    rows = _folded_bucket(
        conn,
        f"""
        WITH scored AS MATERIALIZED (
            SELECT id, sb_match(commitment, ?, ?, ?) AS score FROM commitments
        )
        SELECT c.id, c.email_id, c.commitment, c.by_person, c.to_person,
               e.subject as email_subject, s.score
        FROM scored s
        JOIN commitments c ON c.id = s.id
        LEFT JOIN emails e ON e.id = c.email_id
        WHERE s.score > 0 AND {visible}
        ORDER BY s.score DESC
        LIMIT ?
        """,
        keyword,
        limit,
    )
    # Dated by its email, as an action is: a commitment had no date at all.
    return envelope.extracted(conn, with_source_class(conn, rows), "commitments", "id")


def _search_inline_images(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    if not _table_exists(conn, "inline_images"):
        return []
    rows = _folded_bucket(
        conn,
        """
        WITH scored AS MATERIALIZED (
            SELECT sha256, sb_match(vision_description, ?, ?, ?) AS score FROM inline_images
        )
        SELECT i.sha256, i.classification, i.vision_description, i.width, i.height, s.score
        FROM scored s
        JOIN inline_images i ON i.sha256 = s.sha256
        WHERE s.score > 0
        ORDER BY s.score DESC
        LIMIT ?
        """,
        keyword,
        limit,
    )
    return envelope.images(conn, rows)


def _search_teams(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    """Search teams_threads_fts + teams_messages_fts via store.teams_query.search_teams.

    Returns empty list if the teams_threads_fts table doesn't exist (pre-v12 DB).
    """
    if not _table_exists(conn, "teams_threads_fts"):
        return []
    return envelope.teams(conn, _search_teams_q(conn, keyword, kind="both", limit=limit))


def _search_whatsapp(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    """WhatsApp session summaries and message text; empty on a store from before v26."""
    if not _table_exists(conn, "whatsapp_threads"):
        return []
    return envelope.whatsapp(conn, _search_whatsapp_q(conn, keyword, limit=limit))


def _search_calendar_events(conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    """FTS search over calendar events, leaving out cancelled ones.

    Cancelled by Outlook, or by calendar-sync because Outlook no longer lists the
    event: either way it is not a meeting, and a deleted one kept coming back.
    """
    if not _table_exists(conn, "calendar_events_fts"):
        return []
    try:
        for expression, partial in fts5_query_variants(query, name_forms(conn)):
            rows = conn.execute(
                """SELECT ce.id, ce.subject, ce.start_at, ce.body_summary, ce.organizer_name
                   FROM calendar_events_fts f
                   JOIN calendar_events ce ON ce.id = f.rowid
                   WHERE calendar_events_fts MATCH ? AND ce.is_cancelled = 0
                   ORDER BY rank LIMIT ?""",
                (expression, limit),
            ).fetchall()
            if rows:
                return envelope.calendar(
                    conn, [{**dict(r), "partial_match": True} if partial else dict(r) for r in rows]
                )
        return []
    except sqlite3.OperationalError as e:
        # Was `except Exception: return []`, which turned an unsanitized-query
        # crash into a confident empty calendar bucket. The sanitizer above is
        # the actual fix; this stays narrow and loud so the next tokenizer change
        # is visible instead of silently zeroing a whole result kind.
        logger.warning("recall calendar bucket skipped: %s", e)
        return []


# recall auto-injects a person and a topic dossier as a HINT beside the buckets,
# so they get a tighter cap than a direct person_context/topic_context call. With
# no cap at all these two lines were >95% of every oversized recall payload.
_CONTEXT_HINT_LIMIT = 5


def _maybe_person_context(
    conn: sqlite3.Connection, query: str, days: int, **switches: bool
) -> dict | None:
    """Return person_context if the query means one known person for sure.

    `switches` are include_news and include_automation, as recall was given them.
    """
    # resolve_identity is the test, so for a name recall attaches the person
    # person_context and meeting_prep find: one with a word starting with the
    # query. A substring test found a name for most topics ('AI' inside
    # Michail, 'EU' inside Piraeus) and attached that dossier, and a separate
    # pre-check here disagreed with the resolver in both directions. Not by an
    # address's local part, which a topic word such as 'data' or 'info' starts
    # often enough. And only for an identifier, or a name that fits one person
    # (rows the alias map joins count once): a surname fitting 20 rows was
    # attached as whichever was most emailed, and 2 of the 7 dossiers attached
    # in an audit were the right person on the wrong row. person_context
    # resolves a name it cannot be sure of and says so.
    resolution = resolve_identity(conn, query, local_part=False)
    if resolution.row is None or (
        resolution.resolved_by not in IDENTIFIER_KINDS and resolution.match_count != 1
    ):
        return None
    ctx = get_person_context(
        conn, query, days=days, limit=_CONTEXT_HINT_LIMIT, local_part=False, **switches
    )
    return ctx if ctx.get("person") else None


def _maybe_topic_context(
    conn: sqlite3.Connection, query: str, days: int, **switches: bool
) -> dict | None:
    """Return topic_context if the query plausibly matches a known topic."""
    topic = normalize_topic(query)
    if not any(ch.isalnum() for ch in topic):
        return None  # LIKE '%%' fits every topic, and the most-used one came back
    hit = conn.execute("SELECT 1 FROM topics WHERE name LIKE ?", (f"%{topic}%",)).fetchone()
    if not hit:
        return None
    ctx = get_topic_context(conn, query, days=days, limit=_CONTEXT_HINT_LIMIT, **switches)
    return ctx if ctx.get("topic") else None


# How many semantic candidates a bucket asks for per slot of its pool. Replies of one
# thread embed close together (cosine 0.89 against 0.66 for two random emails), so
# about 9 of 20 candidates were one thread's siblings, which the fusion then collapsed
# into one: the pool held about 11 threads. With room for three per slot, collapsed
# first and cut after, it holds its 20.
SIBLING_HEADROOM = 3


def _key_kind(key: Hashable) -> str | None:
    """What a fusion key of the form (kind, value) is keyed by: thread, series, copy..."""
    return key[0] if isinstance(key, tuple) and key else None


def _dedup(keys: Iterable[Hashable]) -> list[Hashable]:
    return list(dict.fromkeys(keys))


def _fused_keys(*lists: list[Hashable], recency: list[Hashable] | None = None) -> list[Hashable]:
    """The keys of the lists given, by reciprocal rank fusion, best first; a recency list
    counts at ranking.RECENCY_WEIGHT."""
    rankings = [_dedup(keys) for keys in lists]
    weights = [1.0] * len(rankings)
    if recency is not None:
        rankings.append(_dedup(recency))
        weights.append(ranking.RECENCY_WEIGHT)
    return [key for key, _score in reciprocal_rank_fusion(rankings, weights=weights)]


def _semantic_pool(items: Iterable, key: Callable, pool: int) -> dict[Hashable, list]:
    """Semantic items by their bucket's key, best first, for the first `pool` keys: the
    siblings of a key collapse onto it before the pool is cut, not after."""
    by_key: dict[Hashable, list] = {}
    for item in items:
        k = key(item)
        if k is None or (k not in by_key and len(by_key) >= pool):
            continue
        by_key.setdefault(k, []).append(item)
    return by_key


def _more_editions(row: dict, refs: Iterable) -> dict:
    """`row` with the refs semantic search found of its series that it does not list."""
    listed = set(row.get("editions") or ()) | {row.get("ref")}
    extra = [r for r in refs if r not in listed]
    if extra:
        row = dict(row)
        row["editions"] = [*(row.get("editions") or ()), *extra]
        row["older_editions"] = len(row["editions"])
    return row


def _email_keys(conn: sqlite3.Connection, email_ids: list, series: bool) -> dict:
    """email id -> the key the fused email bucket counts it under: its thread, as keyword
    search counts threads, or with `series` its dated subject's series of editions."""
    threads = thread_keys(conn, email_ids)
    keys = {eid: ("thread", t) for eid, t in threads.items() if t}
    if series:
        rows = conn.execute(
            "SELECT id, subject FROM emails WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(email_ids)),),
        )
        for eid, subject in rows:
            name = ranking.series_key(subject, subject=True)
            if name:
                keys[eid] = ("series", name)
    return keys


def _hybrid_emails(
    conn: sqlite3.Connection,
    keyword_hits: list[dict],
    sem_ids: list,
    limit: int,
    pool: int,
    *,
    collapse: bool,
    classes: tuple[str, ...] | None = None,
    **switches: bool,
) -> list[dict]:
    """RRF-fuse keyword email hits with semantic email candidates.

    The keyword hits are an over-fetched pool, so fusion has candidates; the two
    rankings merge by Reciprocal Rank Fusion, then the page is cut to `limit`.
    Semantic-only emails are hydrated as email rows tagged source='semantic'.
    `switches` are include_news and include_automation, and `classes` the source
    classes `kinds` asked for: a candidate of a class they leave out is dropped,
    whatever the provider returned.

    Fused by thread, not by email: keyword search returns one email per thread, and
    the email that embeds best is rarely that one, so a thread both rankings put
    first never got the credit for it. A thread with a keyword hit is shown by that
    hit, which carries the evidence of the match. With `collapse` the candidates
    collapse onto their thread, or their dated subject's series, before the first
    `pool` are kept, and a series' semantic members join its row's `editions`.
    """
    sem_ids = visible_ids(conn, sem_ids, classes=classes, **switches)
    if not sem_ids:
        return keyword_hits[:limit]
    kw_ids = [h["email_id"] for h in keyword_hits]
    keys = _email_keys(conn, kw_ids + list(sem_ids), collapse)

    def key(email_id):
        return keys.get(email_id) or ("email", email_id)

    kw_by_key: dict = {}
    for kw_hit in keyword_hits:
        kw_by_key.setdefault(key(kw_hit["email_id"]), kw_hit)
    sem_by_key = _semantic_pool(sem_ids, key, pool)

    out: list[dict] = []
    for k in _fused_keys(list(kw_by_key), list(sem_by_key)):
        if len(out) >= limit:
            break
        hit = kw_by_key.get(k)
        members = sem_by_key.get(k, [])
        if hit is not None:
            if collapse and _key_kind(k) == "series":
                hit = _more_editions(hit, [m for m in members if m != hit["email_id"]])
            out.append(hit)
            continue
        rows = {
            r["email_id"]: dict(r)
            for r in conn.execute(
                """
                SELECT id as email_id, date_received as date, subject,
                       summary, summary as snippet
                FROM emails WHERE id IN (SELECT value FROM json_each(?))
                """,
                (json.dumps(members),),
            )
        }
        found = [rows[m] for m in (members if collapse else members[:1]) if m in rows]
        if not found:
            continue
        if collapse and _key_kind(k) == "series":
            found.sort(key=lambda r: r["date"] or "", reverse=True)
            hit = found[0]
            if len(found) > 1:
                hit["editions"] = [r["email_id"] for r in found[1:]]
                hit["older_editions"] = len(found) - 1
        else:
            hit = found[0]
        hit["source"] = "semantic"
        out.append(hit)
    with_source_class(conn, out)
    # The keyword hits came with their envelope; the semantic ones get theirs here.
    envelope.emails(conn, [hit for hit in out if "ref" not in hit])
    return out


def _attachment_rows(conn: sqlite3.Connection, attachment_ids: list) -> dict[int, dict]:
    """attachments.id -> a row as search_attachments writes one, with no snippet: what a
    semantic hit is shown as. A row whose text was never summarised has no vector."""
    rows = conn.execute(
        """
        SELECT a.id, a.filename, a.mime_type, a.email_id, e.subject, e.date_received,
               ac.summary, a.sha256
        FROM attachments a
        LEFT JOIN emails e ON e.id = a.email_id
        LEFT JOIN attachment_content ac ON ac.attachment_id = a.id
        WHERE a.id IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(attachment_ids),),
    )
    out: dict[int, dict] = {}
    for r in rows:
        out.setdefault(
            r[0],
            {
                "attachment_id": r[0],
                "filename": r[1],
                "mime_type": r[2],
                "email_id": r[3],
                "email_subject": r[4],
                "email_date": r[5],
                "summary": r[6],
                "sha256": r[7],
                "matched_in": envelope.SEMANTIC,
            },
        )
    return out


def _attachment_key(row: dict) -> Hashable:
    """The attachment bucket's key: a dated or versioned filename's series, else the
    file's hash (copies), else the attachment."""
    name = ranking.series_key(row.get("filename"))
    if name:
        return ("series", name)
    if row.get("sha256"):
        return ("copy", row["sha256"])
    return ("attachment", row["attachment_id"])


def _fuse_attachments(
    conn: sqlite3.Connection, keyword_rows: list[dict], sem_ids: list, limit: int, pool: int
) -> list[dict]:
    """The attachment bucket: its keyword rows (already ranked with recency, editions
    collapsed) fused with the attachments semantic search ranks."""
    hashes = dict(
        conn.execute(
            "SELECT id, sha256 FROM attachments WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps([r["attachment_id"] for r in keyword_rows]),),
        ).fetchall()
    )
    kw_by_key: dict[Hashable, dict] = {}
    for row in keyword_rows:
        kw_by_key.setdefault(
            _attachment_key({**row, "sha256": hashes.get(row["attachment_id"])}), row
        )
    hydrated = _attachment_rows(conn, sem_ids)
    sem_by_key = _semantic_pool(
        (hydrated[i] for i in sem_ids if i in hydrated), _attachment_key, pool
    )
    out: list[dict] = []
    for k in _fused_keys(list(kw_by_key), list(sem_by_key))[:limit]:
        members = sem_by_key.get(k, [])
        if k in kw_by_key:
            row = kw_by_key[k]
            if members and _key_kind(k) != "attachment":
                row = _more_editions(
                    row,
                    [m["attachment_id"] for m in members if m["attachment_id"] != row.get("ref")],
                )
            out.append(row)
            continue
        members = sorted(members, key=lambda m: m["email_date"] or "", reverse=True)
        row = {key: value for key, value in members[0].items() if key != "sha256"}
        if len(members) > 1:
            row["editions"] = [m["attachment_id"] for m in members[1:]]
            row["older_editions"] = len(members) - 1
        out.append(row)
    fresh = [r for r in out if "ref" not in r]
    envelope.attachments(conn, with_source_class(conn, fresh))
    return out


def _thread_rows(conn: sqlite3.Connection, kind: str, thread_ids: list) -> dict[int, dict]:
    """thread id -> a Teams or WhatsApp row as their searches write one, with no snippet
    and no matching message: what a semantic hit is shown as."""
    if kind == "teams":
        sql = """
            SELECT t.id, t.chat_id, t.title, t.summary, t.started_at, t.ended_at,
                   t.message_count, t.participant_display_names, c.team_name, c.topic
            FROM teams_threads t JOIN teams_chats c ON c.id = t.chat_id
            WHERE t.id IN (SELECT value FROM json_each(?))
        """
    else:
        sql = """
            SELECT t.id, t.chat_id, t.title, t.summary, t.started_at, t.ended_at,
                   t.message_count, t.participant_names, c.name, c.chat_kind
            FROM whatsapp_threads t JOIN whatsapp_chats c ON c.id = t.chat_id
            WHERE t.id IN (SELECT value FROM json_each(?))
        """
    try:
        rows = conn.execute(sql, (json.dumps(thread_ids),)).fetchall()
    except sqlite3.OperationalError:  # a store without the tables
        return {}
    out = {}
    for r in rows:
        row = {
            "thread_id": r[0],
            "chat_id": r[1],
            "title": r[2],
            "summary": r[3],
            "started_at": r[4],
            "ended_at": r[5],
            "message_count": r[6],
            "participants": json.loads(r[7] or "[]"),
            "matched_in": envelope.SEMANTIC,
        }
        if kind == "teams":
            row.update(team_name=r[8], channel_topic=r[9])
        else:
            row.update(chat_name=r[8], chat_kind=r[9])
        out[r[0]] = row
    return out


def _fuse_threads(
    conn: sqlite3.Connection,
    kind: str,
    keyword_rows: list[dict],
    sem_ids: list,
    limit: int,
    pool: int,
) -> list[dict]:
    """The Teams or WhatsApp bucket: its keyword rows, newest first as their searches
    order them, fused with the threads semantic search ranks."""
    kw_by_key = {row.get("thread_id"): row for row in reversed(keyword_rows)}
    kw_keys = _dedup(row.get("thread_id") for row in keyword_rows)
    hydrated = _thread_rows(conn, kind, sem_ids)
    sem_by_key = _semantic_pool(
        (hydrated[i] for i in sem_ids if i in hydrated), lambda r: r["thread_id"], pool
    )
    out = []
    for k in _fused_keys(kw_keys, list(sem_by_key))[:limit]:
        out.append(kw_by_key[k] if k in kw_by_key else sem_by_key[k][0])
    fresh = [r for r in out if "ref" not in r]
    (envelope.teams if kind == "teams" else envelope.whatsapp)(conn, fresh)
    return out


def _fuse_conversations(
    conn: sqlite3.Connection, keyword_rows: list[dict], sem_ids: list, limit: int, pool: int
) -> list[dict]:
    """The conversation bucket: its keyword rows and their recency, fused with the
    owner's sessions semantic search ranks."""
    rows = conn.execute(
        """
        SELECT id, session_id, started_at, project_name, summary, turn_count
        FROM conversations WHERE id IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(sem_ids),),
    ).fetchall()
    hydrated = {
        r[0]: {
            "conversation_id": r[0],
            "session_id": r[1],
            "date": r[2],
            "project_name": r[3],
            "summary": r[4],
            "turn_count": r[5],
            "matched_in": envelope.SEMANTIC,
        }
        for r in rows
    }
    kw_by_key = {row["conversation_id"]: row for row in reversed(keyword_rows)}
    kw_keys = _dedup(row["conversation_id"] for row in keyword_rows)
    by_date = sorted(kw_keys, key=lambda k: kw_by_key[k].get("date") or "", reverse=True)
    sem_by_key = _semantic_pool(
        (hydrated[i] for i in sem_ids if i in hydrated), lambda r: r["conversation_id"], pool
    )
    out = []
    for k in _fused_keys(kw_keys, list(sem_by_key), recency=by_date)[:limit]:
        out.append(kw_by_key[k] if k in kw_by_key else sem_by_key[k][0])
    envelope.conversations(conn, [r for r in out if "ref" not in r])
    return out


# The kinds an extracted item's parent can be besides an email, with the alias the
# decision and action buckets join it under, in the order `source` takes them.
_PARENT_ALIASES = (("teams", "tt"), ("whatsapp", "wt"), ("calendar", "ce"), ("conversation", "c"))


def _email_parent_sql(
    conn: sqlite3.Connection, classes: tuple[str, ...] | None, news: bool, automation: bool
) -> str:
    """A condition on the email `e` a row was taken from: of one of `classes`, news and
    automation as `news` and `automation` let in; '0' when none is wanted."""
    if not classes:
        return "0"
    return f"(e.id IS NOT NULL AND {visible_sql(conn, 'e.id', news, automation, classes)})"


def _parents_sql(conn: sqlite3.Connection, wanted: set[str], news: bool, automation: bool) -> str:
    """A condition on the e, tt, wt, ce and c joins of the decision and action buckets:
    the item a row was taken from, read in the order its `source` is, is of a wanted
    kind. '0' when it can be of none."""
    parts = []
    classes = classes_for(wanted & set(EMAIL_KINDS))
    if classes:
        parts.append(_email_parent_sql(conn, classes, news, automation))
    before = ["e.id IS NULL"]
    for kind, alias in _PARENT_ALIASES:
        if kind in wanted:
            parts.append("(" + " AND ".join([*before, f"{alias}.id IS NOT NULL"]) + ")")
        before.append(f"{alias}.id IS NULL")
    return "(" + " OR ".join(parts) + ")" if parts else "0"


def _and(condition: str, narrower: str | None) -> str:
    return condition if narrower is None else f"{condition} AND {narrower}"


# How many buckets recall runs at once, each on its own read-only connection, the
# semantic half first so its embedding call overlaps the keyword buckets.
# BRAIN_RECALL_WORKERS=1 runs them one after another, as before.
RECALL_WORKERS = 6


def recall_workers() -> int:
    """BRAIN_RECALL_WORKERS, else RECALL_WORKERS; at least 1."""
    raw = os.environ.get("BRAIN_RECALL_WORKERS", "").strip()
    try:
        return max(1, int(raw)) if raw else RECALL_WORKERS
    except ValueError:
        return RECALL_WORKERS


def _store_file(conn: sqlite3.Connection) -> str | None:
    """The main database's file, or None for one in memory."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or None
    return None


Task = Callable[[sqlite3.Connection], Any]


def _run(conn: sqlite3.Connection, tasks: list[tuple[str, Task]], workers: int) -> dict:
    """Each task's result by name.

    In parallel, each task on its own read-only connection to conn's store
    (connect_read_only): a sqlite3 connection is not shared across threads. Serially
    on conn itself for one worker, a store in memory, which no other connection can
    open, and a conn with a transaction open, whose rows no other connection sees.
    Either way a task that raises raises here, the first in the order given.
    """
    path = _store_file(conn) if workers > 1 and len(tasks) > 1 else None
    if path is None or conn.in_transaction:
        return {name: task(conn) for name, task in tasks}

    def on_own_connection(task: Task):
        own = connect_read_only(path, immutable=True)
        try:
            return task(own)
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
        futures = [(name, pool.submit(on_own_connection, task)) for name, task in tasks]
        return {name: future.result() for name, future in futures}


def _semantic_task(provider, query: str, limit: int) -> Task:
    """The semantic provider as a task: (email ids or None, ids by kind, status)."""

    def task(conn: sqlite3.Connection):
        try:
            candidates = provider(conn, query, limit)
            status = getattr(candidates, "semantic", "ok")
            by_kind = dict(getattr(candidates, "by_kind", None) or {})
            # Read once: a provider that yields would be empty the second time.
            return list(candidates), by_kind, status
        except Exception as e:
            logger.warning("recall semantic fusion skipped: %s: %s", type(e).__name__, e)
            return None, {}, f"unavailable: {type(e).__name__}"

    return task


def recall(
    conn: sqlite3.Connection,
    query: str,
    limit_per_kind: int = 5,
    days: int = 365,
    semantic_candidates=None,
    include_context: bool = False,
    *,
    include_news: bool = False,
    include_automation: bool = False,
    kinds: Iterable[str] | None = None,
    include_history: bool = False,
) -> dict:
    """Unified search across every text-bearing index in the brain.

    Args:
        conn: Database connection
        query: Free-text query
        limit_per_kind: Max results per category (default 5)
        days: Lookback window for person/topic context (default 365)
        include_context: Attach the person and topic dossiers the query matches
            (default False). They were always attached, as 27% of an average
            payload, and often for the wrong entity: a topic word resolved to a
            person.
        semantic_candidates: Optional callable (conn, query, limit) -> ranked
            email ids. When provided, the emails bucket becomes a keyword+semantic
            RRF fusion; when None (default) every bucket stays keyword-only. When
            the ids carry `by_kind` (embeddings.SemanticCandidates, which
            semantic_email_candidates returns), the attachment, Teams, WhatsApp and
            conversation buckets fuse those too, from the same query vector.
            Injected by the MCP layer so recall itself carries no embedding
            dependency. With a provider, summary.semantic says whether its half ran:
            'ok', 'keyword_seeded: <exception type>' when the query could not be
            embedded and the provider ranked around its keyword matches, or
            'unavailable: <exception type>' when it failed and every bucket is
            keyword-only.
        include_news, include_automation: Include news items and the owner's
            automation mail, which every bucket and dossier leaves out by
            default (src/store/source_class.py). On a store older than the
            class only news is left out, and summary.source_class says so.
        kinds: Only rows of these kinds (src/store/envelope.py KINDS): a bucket
            no row of which can be one is not searched and comes back empty, the
            emails bucket keeps the email kinds named, and decisions, actions and
            commitments those taken from an item of a kind named. Naming news
            includes it whatever include_news says. None (default) is every kind.

    The buckets run in parallel, each on its own read-only connection, with the
    semantic half first (recall_workers, _run); the result is the serial run's.
    With BRAIN_QUERY_RANKING=relevance only the emails bucket fuses, as before.

    Returns:
        Dict with categorized hits across emails (incl. standalone docs),
        attachments, conversations, decisions, actions, inline_images, teams
        threads and WhatsApp sessions, plus, with include_context, person_context
        and topic_context (None when the query matches no known person or topic).
        Always includes every kind key (empty list if no matches) so callers
        don't have to handle missing keys. Rows drawn from emails carry their
        source_class, and every row the row envelope (src/store/envelope.py); a
        row semantic search alone found says matched_in 'semantic'.
    """
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    wanted = None if kinds is None else set(kinds)
    switches = {
        "include_news": include_news or (wanted is not None and "news" in wanted),
        "include_automation": include_automation,
    }

    def asked(*names: str) -> bool:
        return wanted is None or not wanted.isdisjoint(names)

    classes = classes_for(None if wanted is None else wanted & set(EMAIL_KINDS))
    limit = limit_per_kind
    pool = max(limit * 4, limit)
    fused = ranking.fused_ranking()
    hybrid = semantic_candidates is not None
    # Every bucket with vectors fuses, unless the ranking is relevance alone.
    every_bucket = hybrid and fused
    vector_kinds = ("attachment", "teams", "whatsapp", "conversation")
    others = pool if every_bucket else limit

    # What was decided, given or promised, kept to the kinds of item it was taken
    # from: the conditions are read off conn here, and the tasks only run them.
    news, automation = switches["include_news"], switches["include_automation"]
    parents = None if wanted is None else _parents_sql(conn, wanted, news, automation)
    by_email = None if wanted is None else _email_parent_sql(conn, classes, news, automation)
    on_decisions = _and(visible_sql(conn, "d.email_id", news, automation), parents)
    on_actions = _and(visible_sql(conn, "a.email_id", news, automation), parents)
    on_commitments = _and(visible_sql(conn, "c.email_id", news, automation), by_email)
    # Current items only unless include_history (src/store/consolidate.py).
    current = recall_filters(conn, include_history)
    on_decisions = _and(on_decisions, current["decisions"])
    on_actions = _and(on_actions, current["actions"])
    on_commitments = _and(on_commitments, current["commitments"])

    def conversation_rows(c: sqlite3.Connection) -> list[dict]:
        # search_conversations_keyword sanitizes the raw query itself and falls back
        # to any-token like every other bucket.
        if not _table_exists(c, "conversation_turns_fts"):
            return []
        return envelope.conversations(c, search_conversations_keyword(c, query, limit=others))

    tasks: list[tuple[str, Task]] = []
    if hybrid and (asked(*EMAIL_KINDS) or (every_bucket and asked(*vector_kinds))):
        semantic_pool = pool * SIBLING_HEADROOM if fused else pool
        tasks.append(("semantic", _semantic_task(semantic_candidates, query, semantic_pool)))
    # The two buckets that score every row in Python go first: they take longest.
    if parents != "0":
        tasks.append(("actions", lambda c: _search_actions(c, query, limit, on_actions)))
        tasks.append(("decisions", lambda c: _search_decisions(c, query, limit, on_decisions)))
    if asked(*EMAIL_KINDS):
        # Emails, attachments and standalone docs: query_by_keyword spans emails_fts,
        # key_facts_fts and attachment_content_fts in one call. Over-fetched as the
        # fusion's pool when there is a semantic half.
        email_rows = pool if hybrid else limit
        tasks.append(
            (
                "emails",
                lambda c: query_by_keyword(c, query, limit=email_rows, classes=classes, **switches),
            )
        )
    if asked("attachment"):
        # Attachments as their own kind. They DO feed the emails bucket via
        # query_by_keyword, but last in its source waterfall and deduped by
        # email_id: so an attachment whose parent email also matches is invisible,
        # which is the common case (a one-line "see attached" carrying the real
        # substance). This bucket surfaces the extracted text and LLM summary
        # directly, with the filename the caller needs to cite.
        tasks.append(
            ("attachments", lambda c: search_attachments(c, query, limit=others, **switches))
        )
    if asked("conversation"):
        tasks.append(("conversations", conversation_rows))
    if by_email != "0":
        tasks.append(
            ("commitments", lambda c: _search_commitments(c, query, limit, on_commitments))
        )
    # Inline images are not filtered: an image belongs to every email it appears in,
    # and on the replica none of 7,205 described images appeared in news or
    # automation mail alone.
    if asked("image"):
        tasks.append(("inline_images", lambda c: _search_inline_images(c, query, limit)))
    if asked("teams"):
        tasks.append(("teams", lambda c: _search_teams(c, query, others)))
    if asked("whatsapp"):
        tasks.append(("whatsapp", lambda c: _search_whatsapp(c, query, others)))
    if asked("calendar"):
        tasks.append(("calendar_events", lambda c: _search_calendar_events(c, query, limit)))
    if include_context:
        tasks.append(
            ("person_context", lambda c: _maybe_person_context(c, query, days=days, **switches))
        )
        tasks.append(
            ("topic_context", lambda c: _maybe_topic_context(c, query, days=days, **switches))
        )

    done = _run(conn, tasks, recall_workers())

    sem_ids, by_kind, semantic = done.get("semantic", (None, {}, None))
    emails: list[dict] = done.get("emails", [])
    if hybrid and "emails" in done:
        emails = (
            _hybrid_emails(
                conn, emails, sem_ids, limit, pool, collapse=fused, classes=classes, **switches
            )
            if sem_ids is not None
            else emails[:limit]
        )
    attachments: list[dict] = done.get("attachments", [])
    teams: list[dict] = done.get("teams", [])
    whatsapp: list[dict] = done.get("whatsapp", [])
    conversations: list[dict] = done.get("conversations", [])
    # A bucket not asked for has no task, so nothing in `done`, and stays empty.
    if every_bucket and sem_ids is not None:
        if "attachments" in done:
            attachments = _fuse_attachments(
                conn, attachments, by_kind.get("attachment", []), limit, pool
            )
        if "teams" in done:
            teams = _fuse_threads(conn, "teams", teams, by_kind.get("teams", []), limit, pool)
        if "whatsapp" in done:
            whatsapp = _fuse_threads(
                conn, "whatsapp", whatsapp, by_kind.get("whatsapp", []), limit, pool
            )
        if "conversations" in done:
            conversations = _fuse_conversations(
                conn, conversations, by_kind.get("conversation", []), limit, pool
            )
    else:
        attachments, teams, whatsapp = attachments[:limit], teams[:limit], whatsapp[:limit]
        conversations = conversations[:limit]

    text_kinds = {
        "emails": emails,
        "attachments": attachments,
        "conversations": conversations,
        "decisions": done.get("decisions", []),
        "actions": done.get("actions", []),
        "commitments": done.get("commitments", []),
        "inline_images": done.get("inline_images", []),
        "teams": teams,
        "whatsapp": whatsapp,
        "calendar_events": done.get("calendar_events", []),
    }
    total_hits = sum(len(v) for v in text_kinds.values())
    kinds_with_results = [k for k, v in text_kinds.items() if v]
    # Kinds whose every row came from the any-token fallback: nothing there held
    # the whole query, which a caller reading only the summary could not tell.
    # Semantic rows in a fused bucket carry no flag either way, so they count as
    # neither whole nor partial.
    partial_kinds = [
        k
        for k, v in text_kinds.items()
        if v
        and any(r.get("partial_match") for r in v)
        and all(r.get("partial_match") or _semantic_row(r) for r in v)
    ]

    # The semantic status leads: it says how far to trust everything after it.
    summary: dict = {} if semantic is None else {"semantic": semantic}
    if column_missing(conn):
        # So does this: a store older than the class leaves out news alone.
        summary["source_class"] = MISSING
    summary.update(
        total_hits=total_hits,
        kinds_with_results=kinds_with_results,
        partial_kinds=partial_kinds,
    )
    out: dict = {"query": query, **text_kinds}
    if include_context:
        person_context = done.get("person_context")
        topic_context = done.get("topic_context")
        summary["has_person_context"] = person_context is not None
        summary["has_topic_context"] = topic_context is not None
        out["person_context"] = person_context
        out["topic_context"] = topic_context
    out["summary"] = summary
    return out


def _semantic_row(row: dict) -> bool:
    """Whether semantic search alone put this row in its bucket."""
    return row.get("source") == envelope.SEMANTIC or row.get("matched_in") == envelope.SEMANTIC


# How the MCP tool sends recall's rows. A summary is cut to about this many
# characters, enough to judge a hit by: the whole text is one search or one read
# away, and uncut summaries made every bucket heavy.
SUMMARY_CHARS = 300
_CUT_KEYS = ("summary", "body_summary", "vision_description")
# The match markers and ellipses the full-text snippet() calls add.
_SNIPPET_MARKS = re.compile(r">>>|<<<|\.\.\.|…|[\[\]]")


def _cut(text: str, limit: int = SUMMARY_CHARS) -> str:
    """`text` cut to about `limit` characters, at a word boundary when one is near."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    space = head.rfind(" ")
    if space > limit * 0.8:
        head = head[:space]
    return head.rstrip() + "…"


def _plain(text) -> str:
    """Snippet or field text without match markers, folded and with single spaces."""
    return " ".join(search_fold(_SNIPPET_MARKS.sub(" ", str(text or ""))).split())


def compact_rows(rows: list[dict]) -> list[dict]:
    """Rows as recall sends them: summaries cut to about SUMMARY_CHARS characters,
    and a snippet left out when it only repeats the row's subject, title or summary.

    A keyword match on the subject carried the subject again as its snippet, a
    match on the summary the whole summary: 16% of an email row's characters.
    """
    out = []
    for row in rows:
        row = dict(row)
        snippet = row.get("snippet")
        if isinstance(snippet, str):
            plain = _plain(snippet)
            if not plain or any(
                plain in _plain(row.get(key)) for key in ("subject", "title", "summary")
            ):
                del row["snippet"]
        for key in _CUT_KEYS:
            if isinstance(row.get(key), str):
                row[key] = _cut(row[key])
        out.append(row)
    return out

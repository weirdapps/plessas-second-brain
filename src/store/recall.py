"""Unified recall — single 'ask once, get everything' entry point.

Composes keyword search across every text-bearing index in the brain
(emails+attachments+standalone docs, conversations, decisions, actions,
inline images) and auto-pulls person/topic context when the query matches
a known person name/email or topic. Used by both the /recall skill and
by other agents through the MCP `recall` tool.
"""

import logging
import re
import sqlite3
from collections.abc import Iterable

from src.store import envelope
from src.store.context import get_person_context, get_topic_context
from src.store.conversation_query import search_conversations_keyword
from src.store.envelope import EMAIL_KINDS, classes_for
from src.store.fusion import reciprocal_rank_fusion
from src.store.greek import (
    PHRASE_MATCH,
    register_sql_functions,
    search_fold,
    search_phrase,
    search_tokens,
    search_words,
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
    """
    register_sql_functions(conn)  # the MCP image search calls this directly
    stripped = search_phrase(keyword)
    if not stripped:
        return []
    several = len(stripped.split()) >= 2
    words = "\x1f".join(search_words(keyword)) if several else ""
    tokens = "\x1f".join(search_tokens(keyword)) if several else ""
    rows = [dict(r) for r in conn.execute(sql, (stripped, words, tokens, limit))]
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
        for expression, partial in fts5_query_variants(query):
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


def _hybrid_emails(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    semantic_candidates,
    classes: tuple[str, ...] | None = None,
    **switches: bool,
) -> tuple[list[dict], str]:
    """RRF-fuse keyword email hits with semantic email candidates.

    Over-fetches keyword hits so fusion has a candidate pool, merges the keyword
    and semantic rankings with Reciprocal Rank Fusion, then caps to `limit`.
    Semantic-only emails are hydrated as email rows tagged source='semantic'. Any
    semantic failure (missing index, embed error, no ADC) degrades gracefully to
    keyword-only, so recall never breaks. `switches` are include_news and
    include_automation, and `classes` the source classes `kinds` asked for: a
    candidate of a class they leave out is dropped, whatever the provider returned.

    Returns the rows and the semantic half's status: 'ok', 'keyword_seeded:
    <exception type>' when the provider ranked around the keyword matches because
    the query could not be embedded (its `semantic` attribute says so), or
    'unavailable: <exception type>'. The fallback used to leave no trace, so a
    keyword-only answer, when the Mac's ADC had expired, read exactly like a fused one.
    """
    pool = max(limit * 4, limit)
    keyword_hits = query_by_keyword(conn, query, limit=pool, classes=classes, **switches)
    try:
        candidates = semantic_candidates(conn, query, pool)
        status = getattr(candidates, "semantic", "ok")
        # Read twice below: a provider that yields would be empty the second time.
        sem_ids = list(candidates)
    except Exception as e:
        logger.warning("recall semantic fusion skipped: %s: %s", type(e).__name__, e)
        return keyword_hits[:limit], f"unavailable: {type(e).__name__}"
    sem_ids = visible_ids(conn, sem_ids, classes=classes, **switches)
    if not sem_ids:
        return keyword_hits[:limit], status

    # Fused by thread, not by email: keyword search returns one email per thread,
    # and the email that embeds best is rarely that one, so a thread both
    # rankings put first never got the credit for it. A thread with a keyword
    # hit is shown by that hit, which carries the evidence of the match.
    kw_ids = [h["email_id"] for h in keyword_hits]
    threads = thread_keys(conn, kw_ids + list(sem_ids))

    def key(email_id):
        return threads.get(email_id) or ("email", email_id)

    kw_by_key: dict = {}
    for kw_hit in keyword_hits:
        kw_by_key.setdefault(key(kw_hit["email_id"]), kw_hit)
    sem_by_key: dict = {}
    for email_id in sem_ids:
        sem_by_key.setdefault(key(email_id), email_id)
    fused = reciprocal_rank_fusion([list(kw_by_key), list(sem_by_key)])

    out: list[dict] = []
    for thread, _score in fused:
        if len(out) >= limit:
            break
        hit = kw_by_key.get(thread)
        if hit is not None:
            out.append(hit)
            continue
        row = conn.execute(
            """
            SELECT id as email_id, date_received as date, subject,
                   summary, summary as snippet
            FROM emails WHERE id = ?
            """,
            (sem_by_key[thread],),
        ).fetchone()
        if row:
            hit = dict(row)
            hit["source"] = "semantic"
            out.append(hit)
    with_source_class(conn, out)
    # The keyword hits came with their envelope; the semantic ones get theirs here.
    envelope.emails(conn, [hit for hit in out if "ref" not in hit])
    return out, status


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
            RRF fusion; when None (default) it stays keyword-only. Injected by the
            MCP layer so recall itself carries no embedding dependency. With a
            provider, summary.semantic says whether its half ran: 'ok',
            'keyword_seeded: <exception type>' when the query could not be
            embedded and the provider ranked around its keyword matches, or
            'unavailable: <exception type>' when it failed and the emails bucket
            is keyword-only.
        include_news, include_automation: Include news items and the owner's
            automation mail, which every bucket and dossier leaves out by
            default (src/store/source_class.py). On a store older than the
            class only news is left out, and summary.source_class says so.
        kinds: Only rows of these kinds (src/store/envelope.py KINDS): a bucket
            no row of which can be one is not searched and comes back empty, the
            emails bucket keeps the email kinds named, and decisions, actions and
            commitments those taken from an item of a kind named. Naming news
            includes it whatever include_news says. None (default) is every kind.

    Returns:
        Dict with categorized hits across emails (incl. standalone docs),
        attachments, conversations, decisions, actions, inline_images, teams
        threads and WhatsApp sessions, plus, with include_context, person_context
        and topic_context (None when the query matches no known person or topic).
        Always includes every kind key (empty list if no matches) so callers
        don't have to handle missing keys. Rows drawn from emails carry their
        source_class, and every row the row envelope (src/store/envelope.py).
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
    # Emails (incl. attachments + standalone docs — query_by_keyword spans
    # emails_fts, key_facts_fts, and attachment_content_fts in one call). When a
    # semantic candidate provider is injected (the MCP runtime does this), fuse the
    # keyword and semantic rankings with RRF; otherwise stay keyword-only.
    semantic = None
    emails: list[dict] = []
    if not asked(*EMAIL_KINDS):
        pass
    elif semantic_candidates is None:
        emails = query_by_keyword(conn, query, limit=limit_per_kind, classes=classes, **switches)
    else:
        emails, semantic = _hybrid_emails(
            conn, query, limit_per_kind, semantic_candidates, classes=classes, **switches
        )

    # Conversations. search_conversations_keyword sanitizes the raw query itself
    # and falls back to any-token like every other bucket. It used to be handed
    # an already-sanitized string, which it quoted a second time.
    if asked("conversation") and _table_exists(conn, "conversation_turns_fts"):
        conversations = envelope.conversations(
            conn, search_conversations_keyword(conn, query, limit=limit_per_kind)
        )
    else:
        conversations = []

    # Attachments as their own kind. They DO feed the emails bucket via
    # query_by_keyword, but last in its source waterfall and deduped by
    # email_id — so an attachment whose parent email also matches is invisible,
    # which is the common case (a one-line "see attached" carrying the real
    # substance). This bucket surfaces the extracted text and LLM summary
    # directly, with the filename the caller needs to cite.
    attachments = (
        search_attachments(conn, query, limit=limit_per_kind, **switches)
        if asked("attachment")
        else []
    )

    # What was decided, given or promised, kept to the kinds of item it was taken from.
    news, automation = switches["include_news"], switches["include_automation"]
    parents = None if wanted is None else _parents_sql(conn, wanted, news, automation)
    by_email = None if wanted is None else _email_parent_sql(conn, classes, news, automation)
    decisions: list[dict] = []
    actions: list[dict] = []
    commitments: list[dict] = []
    if parents != "0":
        decisions = _search_decisions(
            conn,
            query,
            limit_per_kind,
            _and(visible_sql(conn, "d.email_id", news, automation), parents),
        )
        actions = _search_actions(
            conn,
            query,
            limit_per_kind,
            _and(visible_sql(conn, "a.email_id", news, automation), parents),
        )
    if by_email != "0":
        commitments = _search_commitments(
            conn,
            query,
            limit_per_kind,
            _and(visible_sql(conn, "c.email_id", news, automation), by_email),
        )
    # Inline images are not filtered: an image belongs to every email it appears in,
    # and on the replica none of 7,205 described images appeared in news or
    # automation mail alone.
    inline_images = _search_inline_images(conn, query, limit_per_kind) if asked("image") else []
    teams = _search_teams(conn, query, limit_per_kind) if asked("teams") else []
    whatsapp = _search_whatsapp(conn, query, limit_per_kind) if asked("whatsapp") else []
    calendar_events = (
        _search_calendar_events(conn, query, limit_per_kind) if asked("calendar") else []
    )

    text_kinds = {
        "emails": emails,
        "attachments": attachments,
        "conversations": conversations,
        "decisions": decisions,
        "actions": actions,
        "commitments": commitments,
        "inline_images": inline_images,
        "teams": teams,
        "whatsapp": whatsapp,
        "calendar_events": calendar_events,
    }
    total_hits = sum(len(v) for v in text_kinds.values())
    kinds_with_results = [k for k, v in text_kinds.items() if v]
    # Kinds whose every row came from the any-token fallback: nothing there held
    # the whole query, which a caller reading only the summary could not tell.
    # Semantic rows in the fused email bucket carry no flag either way, so they
    # count as neither whole nor partial.
    partial_kinds = [
        k
        for k, v in text_kinds.items()
        if v
        and any(r.get("partial_match") for r in v)
        and all(r.get("partial_match") or r.get("source") == "semantic" for r in v)
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
        person_context = _maybe_person_context(conn, query, days=days, **switches)
        topic_context = _maybe_topic_context(conn, query, days=days, **switches)
        summary["has_person_context"] = person_context is not None
        summary["has_topic_context"] = topic_context is not None
        out["person_context"] = person_context
        out["topic_context"] = topic_context
    out["summary"] = summary
    return out


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

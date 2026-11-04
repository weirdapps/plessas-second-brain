"""The MCP tool surface: twelve tools, one envelope, one filter object (issue 172).

The server grew to 28 tools for a workload of three (search, read, count), each with
its own flags. The handlers in src/mcp_server.py stay plain functions and keep their
tests; this module is what a client lists: `recall`, `search`, `read`,
`person_context`, `topic_context`, `meeting_prep`, `query_items`, `figures`,
`sql_query`, `stats`, `outlook_live_search` and `sharepoint_index`, plus the
resources for status and schema beside the pages ones.

Every tool answers in `Result` (src/mcp_results.py): the freshness header, a list
under `result`, and `truncated`, `reason`, `coverage` and `excluded` when they apply.
Every narrowing goes in one `filter` object, the same keys everywhere; a key a tool
cannot honour is refused by name rather than ignored.

Imported at the end of src/mcp_server.py, which registers it.
"""

import json
from typing import Annotated, Any, Literal, TypedDict, cast, get_args

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ConfigDict, Field, with_config

from src import mcp_scope
from src import mcp_server as h
from src.mcp_budget import budget_response
from src.mcp_results import Result
from src.mcp_server import (
    ITEM_PAGE_CHARS,
    ITEM_PAGE_MAX,
    MAX_LIMIT,
    RECALL_MAX_PER_KIND,
    Detail,
    PersonDetail,
    SearchType,
    _tool,
    mcp,
)

Kind = Literal[
    "email",
    "news",
    "document",
    "attachment",
    "teams",
    "whatsapp",
    "calendar",
    "conversation",
    "image",
]


@with_config(ConfigDict(extra="forbid"))
class Filter(TypedDict, total=False):
    """What every tool narrows by; each tool names the keys it honours."""

    person: Annotated[
        str,
        Field(
            description="A name or an address: whom the mail, meetings or decisions involve; "
            "for actions, their owner."
        ),
    ]
    topic: Annotated[str, Field(description="A topic's name.")]
    since: Annotated[
        str, Field(description="YYYY-MM-DD (or an ISO date-time): on or after, Athens time.")
    ]
    until: Annotated[str, Field(description="YYYY-MM-DD (or an ISO date-time), inclusive.")]
    days: Annotated[int, Field(ge=1, le=3650, description="Lookback in days.")]
    kinds: Annotated[
        list[Kind],
        Field(min_length=1, description="Only rows of these kinds, as each row's `kind` says."),
    ]
    chat: Annotated[
        str, Field(description="WhatsApp: a chat's name (part of one matches) or its JID.")
    ]
    workspace: Annotated[str, Field(description="Conversations: part of the project path.")]
    include_news: Annotated[bool, Field(description="Let news items in (left out by default).")]
    include_automation: Annotated[
        bool, Field(description="Let the owner's own automation mail in (left out by default).")
    ]
    include_history: Annotated[
        bool,
        Field(description="Let in items a consolidation pass marked duplicate or superseded."),
    ]
    include_agents: Annotated[
        bool, Field(description="Let agent sessions in (conversations; left out by default).")
    ]


FilterArg = Annotated[
    Filter | None,
    Field(description="Narrow the answer: the keys this tool honours (others are refused)."),
]
_INCLUDES = ("include_news", "include_automation", "include_history")


def _take(tool: str, given: Filter | None, allowed: tuple[str, ...]) -> dict[str, Any]:
    """`given` as a dict, or a ToolError naming each key `tool` cannot honour."""
    found = dict(given or {})
    refused = sorted(set(found) - set(allowed))
    if refused:
        takes = ", ".join(allowed) or "no filter"
        raise ToolError(
            f"{tool}: filter {', '.join(refused)} does not apply here; this call takes {takes}"
        )
    return found


def _as_result(out: Any, key: str | None = None) -> Result:
    """`out` in the one envelope: its list `key` moved to `result` (and in `truncated`
    too), held to the response budget; `scope` named when the server has one."""
    out = dict(out)
    allowed = mcp_scope.scope()
    if allowed is not None:
        out["scope"] = sorted(allowed)
    if key and key in out:
        out["result"] = out.pop(key)
        cut = out.get("truncated")
        if isinstance(cut, dict) and key in cut:
            out["truncated"] = {("result" if k == key else k): v for k, v in cut.items()}
    return cast(Result, budget_response(out))


@_tool(upfront=True, name="recall")
def recall(
    query: Annotated[str, Field(description="Plain words: a name, a topic, a phrase.")],
    filter: FilterArg = None,
    detail: Detail = "full",
    limit_per_kind: Annotated[
        int,
        Field(ge=1, le=RECALL_MAX_PER_KIND, description="Rows per kind, 1 to 10 (default 5)."),
    ] = 5,
    include_context: Annotated[
        bool, Field(description="Attach the person and topic dossiers the query matches.")
    ] = False,
) -> Result:
    """Tell me everything we know about X: one search across every index, the default first call.

    Opens with `summary` (semantic status, kinds_with_results), then buckets: emails,
    attachments, conversations, decisions, actions, commitments, inline_images, teams,
    whatsapp, calendar_events, and `pages` (compiled entity cards). A bucket where no row
    held every word falls back to some of them, flagged partial_match. Filter: days
    (default 365), kinds, include_news, include_automation, include_history. read(ref)
    opens any row.
    """
    mcp_scope.refuse_mixed("recall")
    f = _take("recall", filter, ("days", "kinds", *_INCLUDES))
    return cast(
        Result,
        h.recall(
            query,
            limit_per_kind=limit_per_kind,
            days=f.get("days", 365),
            include_context=include_context,
            include_news=f.get("include_news", False),
            include_automation=f.get("include_automation", False),
            kinds=f.get("kinds"),
            detail=detail,
            include_history=f.get("include_history", False),
        ),
    )


Source = Literal["email", "attachment", "teams", "whatsapp", "conversation", "image", "calendar"]
_EMAIL_KINDS = {"email", "news", "document"}
_SEMANTIC_KINDS = _EMAIL_KINDS | {"attachment", "teams", "whatsapp", "conversation"}
_SEARCH_FILTERS: dict[str, tuple[str, ...]] = {
    "email": (
        "person",
        "topic",
        "since",
        "until",
        "kinds",
        "include_news",
        "include_automation",
    ),
    "attachment": ("include_news", "include_automation"),
    "teams": (),
    "whatsapp": ("chat", "days"),
    "conversation": ("workspace", "include_agents"),
    "image": (),
    "calendar": ("person", "since", "until"),
}


@_tool(upfront=True, name="search")
def search(
    query: Annotated[
        str,
        Field(
            description="Plain words; every word first, then any meaningful word, those rows "
            "flagged partial_match. May be empty for email with a person, topic or date "
            "filter, and for calendar."
        ),
    ] = "",
    source: Annotated[
        Source,
        Field(
            description="What to search: email (mail, news and documents share it), "
            "attachment (their text), teams, whatsapp, conversation (past Claude Code "
            "sessions), image (what a vision model saw), calendar."
        ),
    ] = "email",
    search_type: SearchType = "keyword",
    filter: FilterArg = None,
    limit: Annotated[
        int, Field(ge=1, le=MAX_LIMIT, description="Maximum rows, 1 to 200 (default 20).")
    ] = 20,
) -> Result:
    """Find rows of one source by keyword or, for email and conversation, by meaning; rows under `result`, newest and best first.

    Filter by source: email takes person, topic, since, until (a listing by those, the
    query matched against subject and summary), kinds, include_news, include_automation;
    attachment include_news, include_automation; whatsapp chat, days; conversation
    workspace, include_agents; calendar person, since, until. Semantic email also
    returns attachment, teams, whatsapp and conversation rows (`kinds` narrows them).
    Every row says its `kind` and `ref`: read(ref) opens it.
    """
    f = _take(f"search source={source}", filter, _SEARCH_FILTERS[source])
    semantic = search_type == "semantic"
    if semantic and source not in ("email", "conversation"):
        raise ToolError(
            f"search source={source} is keyword only; semantic is email or conversation"
        )
    if source != "email":
        mcp_scope.require(f"search source={source}", [source])
    words = query.strip()
    if source == "calendar":
        return _as_result(
            h.query_calendar_events(
                person=f.get("person", ""),
                since=f.get("since", ""),
                until=f.get("until", ""),
                keyword=words,
                limit=limit,
            ),
            "events",
        )
    if source == "email":
        kinds = f.get("kinds")
        allowed = _SEMANTIC_KINDS if semantic else _EMAIL_KINDS
        if kinds and not set(kinds) <= allowed:
            raise ToolError(f"search source=email takes kinds {', '.join(sorted(allowed))}")
        offered = [k for k in get_args(Kind) if k in allowed]
        kinds = cast(Any, mcp_scope.narrow(kinds, offered))
        listing = any(f.get(k) for k in ("person", "topic", "since", "until"))
        includes = {
            "include_news": f.get("include_news", False),
            "include_automation": f.get("include_automation", False),
        }
        if listing:
            if semantic:
                raise ToolError(
                    "search source=email with a person, topic or date filter is keyword only"
                )
            return _as_result(
                h.query_emails(
                    person=f.get("person"),
                    topic=f.get("topic"),
                    keyword=words or None,
                    start_date=f.get("since"),
                    end_date=f.get("until"),
                    limit=limit,
                    kinds=kinds,
                    **includes,
                )
            )
        if not words:
            raise ToolError(
                "search source=email needs a query, or a person, topic, since or until filter"
            )
        return _as_result(
            h.search_emails(words, search_type=search_type, limit=limit, kinds=kinds, **includes)
        )
    if not words:
        raise ToolError(f"search source={source} needs a query")
    if source == "attachment":
        return _as_result(
            h.search_attachments(
                words,
                limit=limit,
                include_news=f.get("include_news", False),
                include_automation=f.get("include_automation", False),
            )
        )
    if source == "teams":
        return _as_result(h.search_teams(words, limit=limit), "results")
    if source == "whatsapp":
        return _as_result(
            h.search_whatsapp(words, chat=f.get("chat"), days=f.get("days"), limit=limit),
            "results",
        )
    if source == "conversation":
        return _as_result(
            h.search_conversations(
                words,
                search_type=search_type,
                workspace=f.get("workspace"),
                limit=limit,
                include_agents=f.get("include_agents", False),
            )
        )
    return _as_result(h.attachment_image_search(words, limit=limit), "results")


# Kinds whose ref is text, not a number.
_TEXT_REFS = {"conversation", "image", "page"}


def _parse_ref(ref: str) -> tuple[str, int | str]:
    kind, sep, key = ref.strip().partition(":")
    kinds = (*get_args(Kind), "page")
    if not sep or not key or kind not in kinds:
        raise ToolError(
            f"ref must be <kind>:<ref> as a row gives them (email:4211, "
            f"conversation:<session id>, page:person/12); kind is one of {', '.join(kinds)}; "
            f"got {ref!r}"
        )
    if kind not in _TEXT_REFS and key.isdigit():
        return kind, int(key)
    return kind, key


def _read_page(key: str) -> dict:
    """A compiled page (issue 169) as an item: its markdown is the text."""
    from src.store import pages

    page_kind, sep, entity = key.partition("/")
    conn = h._get_conn()
    try:
        page = pages.get_page(conn, page_kind, entity) if sep else None
        head = h._header(conn)
    finally:
        conn.close()
    if page is None:
        raise ToolError(
            f"no page {key!r}: none compiled, hidden by an override, or no page layer; "
            "brain://pages lists them (kind/key)"
        )
    text = h._inert(page.get("markdown") or "")
    item = {k: v for k, v in page.items() if k != "markdown"}
    return {**head, "kind": "page", "ref": key, "item": h._inert(item), "text": text}


@_tool(upfront=True, name="read")
def read(
    ref: Annotated[
        str,
        Field(
            description="<kind>:<ref> as a row gives them: email:4211, attachment:88, "
            "teams:12, whatsapp:5, calendar:301, conversation:<session id>, "
            "image:<sha256>, page:person/12 (a compiled page)."
        ),
    ],
    part: Annotated[
        str | None,
        Field(
            description="Only one part: 'text', 'item' (the metadata and what was extracted), "
            "or one key of item (thread, attachments, people, decisions...). Omit for both."
        ),
    ] = None,
    offset: Annotated[
        int, Field(ge=0, description="Where in the text to start: the last page's next_offset.")
    ] = 0,
    max_chars: Annotated[
        int,
        Field(
            ge=1000,
            le=ITEM_PAGE_MAX,
            description="Most characters of text per page, 1,000 to 30,000 (default 20,000).",
        ),
    ] = ITEM_PAGE_CHARS,
    filter: FilterArg = None,
) -> Result:
    """Read the item behind any row, whole: an email's body with its thread, people and attachments; an attachment's text; a Teams thread or WhatsApp session; a meeting with every attendee; a past conversation turn by turn; an image's description; a compiled page.

    `item` holds the metadata and what was extracted (decisions, actions, key facts);
    `text` is one page of the text from `offset`, at most `max_chars`, with
    `total_chars` and `next_offset` (null at the end). Read long text here a page at a
    time rather than through sql_query. Filter: include_agents (an agent session).
    """
    f = _take("read", filter, ("include_agents",))
    kind, key = _parse_ref(ref)
    if kind == "page":
        mcp_scope.refuse_mixed("read of a compiled page")
    else:
        mcp_scope.require(f"read {kind}:{key}", [kind])
    if kind == "page":
        found = _read_page(str(key))
        text = found["text"]
        if offset and offset >= len(text):
            raise ToolError(f"offset {offset:,} is past the end ({len(text):,} characters)")
        chunk = text[offset : offset + max_chars]
        end = offset + len(chunk)
        out = {
            **found,
            "text": chunk,
            "offset": offset,
            "returned_chars": len(chunk),
            "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
        }
    else:
        out = dict(
            h.get_item(
                cast(Any, kind),
                key,
                offset=offset,
                max_chars=max_chars,
                include_agents=f.get("include_agents", False),
            )
        )
        # email, news and document share one table: a ref read as one may be another.
        real = (out.get("item") or {}).get("kind")
        if isinstance(real, str) and real != kind:
            mcp_scope.require(f"read {kind}:{key} (a {real} row)", [real])
    if part is None:
        return _as_result(out)
    text_keys = ("text", "offset", "returned_chars", "total_chars", "next_offset")
    if part == "text":
        out.pop("item", None)
    elif part == "item":
        for name in text_keys:
            out.pop(name, None)
    else:
        item = out.get("item") or {}
        if part not in item:
            raise ToolError(
                f"read: {ref} has no part {part!r}; it has text, item, {', '.join(sorted(item))}"
            )
        for name in text_keys:
            out.pop(name, None)
        out["item"] = {part: item[part]}
    return _as_result(out)


_DOSSIER = ("days", *_INCLUDES)


@_tool(name="person_context")
def person_context(
    name_or_email: Annotated[
        str,
        Field(
            description="The address when you have one (exact), else a name: case, accent and "
            "script blind ('Papadopoulos' finds Παπαδόπουλος), each word at the start of a "
            "word in any order, a short last word an initial."
        ),
    ],
    detail: PersonDetail = "full",
    limit: Annotated[
        int, Field(ge=1, le=MAX_LIMIT, description="Rows per list, 1 to 200 (default 20).")
    ] = 20,
    filter: FilterArg = None,
) -> Result:
    """Brief me on a person: email history, topics, sentiment, decisions, open actions, communication pattern, meetings, Teams and WhatsApp, and their compiled `page`.

    Read resolved_by, match_count and other_candidates before trusting a name match.
    Filter: days (default 365), include_news, include_automation, include_history.
    """
    mcp_scope.refuse_mixed("person_context")
    f = _take("person_context", filter, _DOSSIER)
    return cast(
        Result,
        h.person_context(
            name_or_email,
            days=f.get("days", 365),
            limit=limit,
            include_news=f.get("include_news", False),
            include_automation=f.get("include_automation", False),
            detail=detail,
            include_history=f.get("include_history", False),
        ),
    )


@_tool(name="topic_context")
def topic_context(
    topic: Annotated[str, Field(description="A topic's name or words of it.")],
    detail: Annotated[
        Literal["full", "concise", "page"],
        Field(
            description="'full' (default) the dossier; 'concise' one short line per row; "
            "'page' where a live topic stands, a page a model rebuilt nightly, each line "
            "citing its items (derived: check with read)."
        ),
    ] = "full",
    limit: Annotated[
        int, Field(ge=1, le=MAX_LIMIT, description="Rows per list, 1 to 200 (default 20).")
    ] = 20,
    filter: FilterArg = None,
) -> Result:
    """Brief me on a topic: related emails, key people, decisions, open actions, key facts and its compiled `page`; or, with detail='page', where it stands now.

    Filter: days (default 365), include_news, include_automation, include_history.
    """
    mcp_scope.refuse_mixed("topic_context")
    if detail == "page":
        _take("topic_context detail=page", filter, ())
        return cast(Result, h.topic_page(topic))
    f = _take("topic_context", filter, _DOSSIER)
    return cast(
        Result,
        h.topic_context(
            topic,
            days=f.get("days", 365),
            limit=limit,
            include_news=f.get("include_news", False),
            include_automation=f.get("include_automation", False),
            detail=detail,
            include_history=f.get("include_history", False),
        ),
    )


@_tool(name="meeting_prep")
def meeting_prep(
    people: Annotated[
        str,
        Field(
            description="The attendees, separated by commas: each one's address when you have "
            "it (exact), else the name."
        ),
    ],
    detail: Detail = "full",
    filter: FilterArg = None,
) -> Result:
    """Prepare me for a meeting: a dossier per attendee with their compiled page, the next and last meetings with them, the series pages, and the topic's dossier when filter.topic is given.

    Filter: topic, days (default 365), include_news, include_automation, include_history.
    """
    mcp_scope.refuse_mixed("meeting_prep")
    f = _take("meeting_prep", filter, ("topic", *_DOSSIER))
    return cast(
        Result,
        h.meeting_prep(
            people,
            topic=f.get("topic"),
            days=f.get("days", 365),
            include_news=f.get("include_news", False),
            include_automation=f.get("include_automation", False),
            detail=detail,
            include_history=f.get("include_history", False),
        ),
    )


_ITEM_FILTERS: dict[str, tuple[str, ...]] = {
    "decisions": ("topic", "person", *_DOSSIER),
    "actions": ("person", *_INCLUDES),
    "stale": ("days", *_INCLUDES),
}


@_tool(name="query_items")
def query_items(
    kind: Annotated[
        Literal["decisions", "actions", "stale"],
        Field(
            description="'decisions' (what was decided), 'actions' (what is to be done, by "
            "whom) or 'stale' (threads you sent last with no answer, and overdue actions)."
        ),
    ],
    status: Annotated[
        Literal["open", "expired", "done"],
        Field(
            description="actions only: 'open' (default), 'expired' (180 days past its deadline "
            "or, with none, 90 days after its source went quiet) or 'done' (closed_by, "
            "closed_at)."
        ),
    ] = "open",
    older_than_days: Annotated[
        int,
        Field(ge=1, le=365, description="stale only: days since your last message (default 5)."),
    ] = 5,
    limit: Annotated[
        int, Field(ge=1, le=MAX_LIMIT, description="Rows per list, 1 to 200 (default 20).")
    ] = 20,
    filter: FilterArg = None,
) -> Result:
    """What was decided, what is owed, who owes me a reply: decisions, action items or stale threads, newest first.

    Filter: decisions take topic, person, days (default 365); actions person (the owner);
    stale days (the oldest thread worth a reminder, default 30); all take include_news,
    include_automation, include_history. Rows under `result`; stale gives
    `stale_threads` and `overdue_actions`.
    """
    mcp_scope.refuse_mixed("query_items")
    f = _take(f"query_items kind={kind}", filter, _ITEM_FILTERS[kind])
    if status != "open" and kind != "actions":
        raise ToolError("query_items: status applies to kind=actions only")
    includes = {k: f.get(k, False) for k in _INCLUDES}
    if kind == "decisions":
        return _as_result(
            h.query_decisions(
                topic=f.get("topic"),
                person=f.get("person"),
                days=f.get("days", 365),
                limit=limit,
                **includes,
            )
        )
    if kind == "actions":
        return _as_result(
            h.query_actions(owner=f.get("person"), status=status, limit=limit, **includes)
        )
    return _as_result(
        h.stale_threads(days=older_than_days, limit=limit, max_days=f.get("days", 30), **includes)
    )


@_tool(upfront=True, name="sql_query")
def sql_query(
    sql: Annotated[
        str | None,
        Field(
            description="A single SELECT; inline the literals. Omit it for the schema: every "
            "table with its row count, or `table`'s columns and indexes."
        ),
    ] = None,
    table: Annotated[
        str | None,
        Field(description="With no sql: the table whose columns and indexes to list."),
    ] = None,
    limit: Annotated[
        int, Field(ge=1, le=MAX_LIMIT, description="Maximum rows, 1 to 200 (default 200).")
    ] = 200,
) -> Result:
    """Count, aggregate, or read the full text the other tools only summarise, with ONE read-only SELECT against brain.db; with no sql, the schema.

    Key columns (sql_query(table=...) has the rest; there is no `date` column):
    emails.date_received, subject, sender_name, sender_address, mailbox_name,
    source_class, summary, content, conversation_id; attachment_content.extracted_text
    and summary (join attachments.id = attachment_content.attachment_id, and
    attachments.email_id = emails.id); teams_messages.composed_at,
    sender_display_name, content_text, thread_id; calendar_events.start_at, end_at,
    subject, with event_attendees.event_id, name, email; decisions (decision,
    decided_by, decision_date) and action_items (task, owner, deadline, status) name
    their parent by email_id, teams_thread_id, whatsapp_thread_id, event_id or
    conversation_turn_id; whatsapp_messages.sent_at, sender_name, content.

    Words in text: use the full-text tables, not LIKE '%...%' scans, e.g. `SELECT
    e.id, e.date_received, e.subject FROM emails_fts JOIN emails e ON e.id =
    emails_fts.rowid WHERE emails_fts MATCH sb_fold('κάρτες')`; word* is a prefix.
    Also attachment_content_fts (rowid = attachment_content.id), teams_messages_fts,
    whatsapp_messages_fts, calendar_events_fts, conversation_turns_fts.
    sb_fold(text) folds case, Greek accents and final ς as the indexes do.

    Read-only. One statement per call (WITH ... SELECT is fine), a 10 s budget, at
    most `limit` rows (cap 200), each cell cut to 4,000 characters (a cut cell ends
    "… [cut, N chars]") and the whole answer to 30,000; header and JSON included, a
    result stays under 40,000 characters, and `truncated` says when something was
    left out. Binary values come back as "<N bytes>". A result wider than 32 columns
    is refused, and so is a query that reads or builds a value over 8 MiB: page long
    text with read.
    """
    mcp_scope.refuse_mixed("sql_query")
    if sql is None:
        return cast(Result, h.sql_schema(table))
    if table is not None:
        raise ToolError("sql_query: pass sql or table, not both")
    return cast(Result, h.sql_query(sql, limit=limit))


# --- MCP resources: status and schema, beside the pages (src/mcp_server.py) -------------


@mcp.resource(
    "brain://status",
    name="status",
    description="What stats returns: counts per source, freshness and each source's coverage.",
    mime_type="application/json",
)
def status_resource() -> str:
    mcp_scope.refuse_mixed("brain://status", resource=True)
    return json.dumps(h.stats(), ensure_ascii=False, default=str)


@mcp.resource(
    "brain://schema",
    name="schema",
    description="Every table of brain.db with its row count, for sql_query.",
    mime_type="application/json",
)
def schema_resource() -> str:
    mcp_scope.refuse_mixed("brain://schema", resource=True)
    return json.dumps(h.sql_schema(None), ensure_ascii=False, default=str)


@mcp.resource(
    "brain://schema/{table}",
    name="table",
    description="One table's columns and indexes, for sql_query.",
    mime_type="application/json",
)
def table_resource(table: str) -> str:
    mcp_scope.refuse_mixed("brain://schema/{table}", resource=True)
    try:
        return json.dumps(h.sql_schema(table), ensure_ascii=False, default=str)
    except ToolError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

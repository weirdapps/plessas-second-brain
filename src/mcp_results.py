"""Return types of the MCP tools.

A tool that returns a plain dict has no output schema, so the SDK sent it only as
JSON text indented by two spaces, 12-33% of it whitespace. Typed, it also goes out
as compact `structuredContent` with an `outputSchema` saying what each key is.

The types name the keys a caller can rely on and stay loose inside them: rows are
dicts of any shape, and keys not listed here still go through (`extra="allow"`),
so a key a store function adds later is never dropped. Every key is optional
(`total=False`) unless the tool always returns it.

Every type but recall's inherits Fresh, so every result opens with `data_as_of`
and `stale`, required: a tool that left them out would fail its own output
validation rather than drift. recall declares them itself, after its summary.
"""

from typing import Any, NotRequired, TypedDict

from pydantic import ConfigDict, with_config

Rows = list[dict[str, Any]]


class Cut(TypedDict):
    """How many rows of one list a response budget kept, of how many."""

    kept: int
    total: int


# Each list the budget cut, by its key or dotted path: {"emails": {"kept", "total"}}.
Truncated = dict[str, Cut]

# What the sources an empty answer searched hold, by source, as stats.coverage gives
# them: {"teams": {"first", "last", "items"}, "mailboxes": {name: {...}}, ...}.
Coverage = dict[str, Any]


class LeftOut(TypedDict):
    """How many matches of one class the default filter left out, and the switch that
    lets them in; `at_least` when the count stopped at its probe's limit."""

    rows: int
    switch: str
    at_least: NotRequired[bool]


# By class: {"news": {"rows": 12, "switch": "include_news=true"}}.
Excluded = dict[str, LeftOut]

_OPEN = ConfigDict(extra="allow")


@with_config(_OPEN)
class Fresh(TypedDict):
    """What every result opens with: how current the store it was read from is.

    `data_as_of` and `stale` as src/store/query.py get_freshness reads them (the older
    of the last sync and the last mail export), and `_stale_warning`, the sentence to
    pass on, when the store is behind. A live read is as of the call.
    """

    data_as_of: str | None
    stale: bool
    _stale_warning: NotRequired[str]


@with_config(_OPEN)
class RowsResult(Fresh):
    """A list tool's rows, with `truncated` when the budget cut them and `reason`
    when nothing matched."""

    excluded: NotRequired[Excluded]
    result: Rows
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]


@with_config(_OPEN)
class ResultsResult(Fresh):
    """search_teams and search_whatsapp: rows under `results`."""

    results: Rows
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]


@with_config(_OPEN)
class PersonContext(Fresh, total=False):
    person: dict[str, Any] | None
    resolved_by: str | None
    match_count: int
    other_candidates: list[Any]
    email_count: int
    recent_emails: Rows
    topics: Rows
    topics_total: int
    sentiment_distribution: dict[str, Any]
    decisions: Rows
    decisions_total: int
    open_actions: Rows
    open_actions_total: int
    communication_pattern: dict[str, Any]
    last_met: Any
    next_meeting: Any
    meeting_count_30d: Any
    teams: dict[str, Any]
    whatsapp: dict[str, Any]
    reason: str
    coverage: Coverage
    truncated: Truncated
    # detail="card": the card sender_brief gave, keyed as SenderBrief is.
    known: bool


@with_config(_OPEN)
class TopicPage(Fresh, total=False):
    """topic_page: the page (None when the topic has none), the findings filed about the
    topic, and, with no page, the topics that have one."""

    topic_page: dict[str, Any] | None
    findings: Rows
    pages: Rows
    reason: str
    truncated: Truncated


@with_config(_OPEN)
class TopicContext(Fresh, total=False):
    topic_page: dict[str, Any]
    topic: dict[str, Any] | None
    email_count: int
    recent_emails: Rows
    key_people: Rows
    key_people_total: int
    decisions: Rows
    decisions_total: int
    open_actions: Rows
    open_actions_total: int
    key_facts: list[Any]
    key_facts_total: int
    reason: str
    coverage: Coverage
    truncated: Truncated


@with_config(_OPEN)
class SenderBrief(Fresh, total=False):
    known: bool
    query: str
    name: Any
    email: Any
    role: Any
    email_count: int
    top_topics: list[Any]
    recent_decisions: int
    open_actions_count: int
    last_contact: Any
    sentiment: dict[str, Any]
    resolved_by: str | None
    match_count: int
    other_candidates: list[Any]
    reason: str
    coverage: Coverage


@with_config(_OPEN)
class EmailThread(Fresh):
    email_id: int
    emails: Rows
    thread_total: int
    truncated: NotRequired[Truncated]


@with_config(_OPEN)
class RecallResult(TypedDict):
    """In this order on the wire: the header that says how far to trust the rest,
    then the buckets."""

    summary: dict[str, Any]
    data_as_of: Any
    stale: bool
    _stale_warning: NotRequired[str]
    truncated: NotRequired[Truncated]
    excluded: NotRequired[Excluded]
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]
    query: str
    emails: Rows
    attachments: Rows
    conversations: Rows
    decisions: Rows
    actions: Rows
    commitments: Rows
    inline_images: Rows
    teams: Rows
    whatsapp: Rows
    calendar_events: Rows
    person_context: NotRequired[dict[str, Any] | None]
    topic_context: NotRequired[dict[str, Any] | None]


@with_config(_OPEN)
class StaleThreads(Fresh):
    stale_threads: Rows
    stale_threads_total: int
    overdue_actions: Rows
    overdue_actions_total: int
    stale_threads_unavailable: NotRequired[str]
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]
    truncated: NotRequired[Truncated]


@with_config(_OPEN)
class MeetingPrep(Fresh, total=False):
    attendees: Rows
    meetings: dict[str, Rows]
    topic_context: dict[str, Any] | None
    reason: str
    coverage: Coverage
    truncated: Truncated


@with_config(_OPEN)
class CalendarEvents(Fresh):
    events: Rows
    count: int
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]


@with_config(_OPEN)
class Stats(Fresh, total=False):
    total_emails: int
    total_news_articles: int
    total_documents: int
    total_topics: int
    total_people: int
    total_decisions: int
    total_action_items: int
    earliest_email: Any
    latest_email: Any
    source_class: dict[str, int] | str
    coverage: dict[str, Any]
    # data_as_of and stale come from Fresh, first.
    age_hours: Any
    stale_warning: str
    total_attachments: int
    attachments_extracted: int
    attachments_awaiting_text: int
    attachments_awaiting_text_oldest_hours: float | None
    attachments_awaiting_summary: int
    attachments_awaiting_summary_oldest_hours: float | None
    total_key_facts: int
    total_conversations: int
    total_conversation_turns: int
    embed_backend: str
    last_embed_error: dict[str, str] | None


@with_config(_OPEN)
class ConversationContext(Fresh, total=False):
    session_id: str
    kind: str
    summary: Any
    turns: Rows
    decisions: Rows
    action_items: Rows
    key_facts: list[Any]
    topics: list[Any]
    truncated: Truncated


@with_config(_OPEN)
class SharepointIndex(Fresh, total=False):
    links: Rows
    status: str
    document_message_id: Any
    reason: str
    coverage: Coverage
    truncated: Truncated


@with_config(_OPEN)
class ImageSearch(Fresh):
    results: Rows
    count: int
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]
    truncated: NotRequired[Truncated]


@with_config(_OPEN)
class LiveMail(Fresh):
    messages: Rows
    count: int
    since_minutes: int
    clamped: bool
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]
    truncated: NotRequired[Truncated]


@with_config(_OPEN)
class TeamsThread(Fresh, total=False):
    thread: dict[str, Any]
    messages: Rows
    decisions: Rows
    action_items: Rows
    key_facts: Rows
    truncated: Truncated


@with_config(_OPEN)
class TeamsChat(Fresh, total=False):
    chat: dict[str, Any]
    threads: Rows
    last_messages: Rows
    top_senders: Rows
    open_actions: Rows
    reason: str
    coverage: Coverage
    truncated: Truncated


@with_config(_OPEN)
class SqlResult(Fresh):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: Any
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]


@with_config(_OPEN)
class ItemResult(Fresh):
    """get_item: the item behind a row's ref, its text a page at a time."""

    kind: str
    ref: Any
    item: dict[str, Any]
    text: str
    offset: int
    returned_chars: int
    total_chars: int
    next_offset: int | None
    truncated: NotRequired[Truncated]


@with_config(_OPEN)
class FiguresResult(Fresh):
    """figures: one row per matching metric, its latest held value and the one before it, or
    in mode 'series' one row per report series; `reason` when a list is short or the period
    asked is not held, `coverage` what the registry holds when nothing matched."""

    mode: str
    result: Rows
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]
    coverage: NotRequired[Coverage]


@with_config(_OPEN)
class SqlSchema(Fresh, total=False):
    tables: Rows
    table: str
    columns: Rows
    indexes: Rows

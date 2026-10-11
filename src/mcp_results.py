"""Return types of the MCP tools.

A tool that returns a plain dict has no output schema, so the SDK sent it only as
JSON text indented by two spaces, 12-33% of it whitespace. Typed, it also goes out
as compact `structuredContent` with an `outputSchema` saying what each key is.

The types name the keys a caller can rely on and stay loose inside them: rows are
dicts of any shape, and keys not listed here still go through (`extra="allow"`),
so a key a store function adds later is never dropped. Every key is optional
(`total=False`) unless the tool always returns it.
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

_OPEN = ConfigDict(extra="allow")


@with_config(_OPEN)
class RowsResult(TypedDict):
    """A list tool's rows, with `truncated` when the budget cut them and `reason`
    when nothing matched."""

    result: Rows
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]


@with_config(_OPEN)
class ResultsResult(TypedDict):
    """search_teams and search_whatsapp: rows under `results`."""

    results: Rows
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]


@with_config(_OPEN)
class PersonContext(TypedDict, total=False):
    person: dict[str, Any] | None
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


@with_config(_OPEN)
class TopicContext(TypedDict, total=False):
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


@with_config(_OPEN)
class SenderBrief(TypedDict, total=False):
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
    match_count: int
    other_candidates: list[Any]


@with_config(_OPEN)
class EmailThread(TypedDict):
    email_id: int
    emails: Rows
    thread_total: int


@with_config(_OPEN)
class RecallResult(TypedDict):
    """In this order on the wire: the header that says how far to trust the rest,
    then the buckets."""

    summary: dict[str, Any]
    data_as_of: Any
    stale: bool
    _stale_warning: NotRequired[str]
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]
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
class StaleThreads(TypedDict):
    stale_threads: Rows
    stale_threads_total: int
    overdue_actions: Rows
    overdue_actions_total: int
    stale_threads_unavailable: NotRequired[str]


@with_config(_OPEN)
class MeetingPrep(TypedDict, total=False):
    attendees: Rows
    topic_context: dict[str, Any] | None


@with_config(_OPEN)
class CalendarEvents(TypedDict):
    events: Rows
    count: int
    truncated: NotRequired[Truncated]
    reason: NotRequired[str]


@with_config(_OPEN)
class Stats(TypedDict, total=False):
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
    data_as_of: Any
    age_hours: Any
    stale: bool
    stale_warning: str
    total_attachments: int
    attachments_extracted: int
    total_key_facts: int
    total_conversations: int
    total_conversation_turns: int
    embed_backend: str
    last_embed_error: dict[str, str] | None


@with_config(_OPEN)
class ConversationContext(TypedDict, total=False):
    session_id: str
    summary: Any
    turns: Rows
    decisions: Rows
    action_items: Rows
    key_facts: list[Any]
    topics: list[Any]


@with_config(_OPEN)
class SharepointIndex(TypedDict, total=False):
    links: Rows
    status: str
    document_message_id: Any


@with_config(_OPEN)
class ImageSearch(TypedDict):
    results: Rows
    count: int
    reason: NotRequired[str]


@with_config(_OPEN)
class LiveMail(TypedDict):
    messages: Rows
    count: int
    since_minutes: int
    clamped: bool


@with_config(_OPEN)
class TeamsThread(TypedDict, total=False):
    thread: dict[str, Any]
    messages: Rows
    decisions: Rows
    action_items: Rows
    key_facts: Rows


@with_config(_OPEN)
class TeamsChat(TypedDict, total=False):
    chat: dict[str, Any]
    threads: Rows
    last_messages: Rows
    top_senders: Rows
    open_actions: Rows


@with_config(_OPEN)
class SqlResult(TypedDict):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: Any


@with_config(_OPEN)
class SqlSchema(TypedDict, total=False):
    tables: Rows
    table: str
    columns: Rows
    indexes: Rows

"""The MCP handlers bound every limit and reject inputs they cannot honour.

Only email_thread and stale_threads clamped `limit`. SQLite reads a negative
LIMIT as none, so limit=-1 dumped every row; keyword search_emails stopped at
`len(results) >= limit` and returned nothing for limit <= 0. An unknown
search_type silently ran keyword search, an unknown Teams kind returned
nothing, and a date that is not ISO excluded every email. Each read as
'nothing found'.
"""

import importlib

import pytest

from src import mcp_server
from src.store.schema import create_database, get_connection


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    for n in range(1, 4):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary, "
            "conversation_id) VALUES (?, ?, ?, ?, 'the okapi plan', ?)",
            (n, n, f"2026-09-0{n}T10:00:00Z", f"Okapi {n}", f"T{n}"),
        )
        conn.execute(
            "INSERT INTO attachments (id, email_id, message_id, filename, file_path, "
            "exported_at, mime_type, file_size) VALUES (?, ?, ?, ?, '/x', '2026-09-01', "
            "'application/pdf', 1)",
            (n, n, n, f"okapi{n}.pdf"),
        )
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status) "
            "VALUES (?, 'the okapi figures', 'done')",
            (n,),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))
    return path


# (handler, its arguments, where the limit lands, the store function's limit argument)
CASES = [
    ("person_context", {"name_or_email": "x"}, "src.store.context:get_person_context", "limit"),
    ("topic_context", {"topic": "okapi"}, "src.store.context:get_topic_context", "limit"),
    ("email_thread", {"email_id": 1}, "src.store.query:query_thread", "limit"),
    ("search_emails", {"query": "okapi"}, "src.store.query:query_by_keyword", "limit"),
    (
        "search_emails",
        {"query": "okapi", "search_type": "semantic"},
        "src.store.embeddings:query_semantic",
        "limit",
    ),
    ("recall", {"query": "okapi"}, "src.store.recall:recall", "limit_per_kind"),
    ("query_emails", {"keyword": "okapi"}, "src.store.query:query_combined", "limit"),
    ("query_decisions", {}, "src.store.query:query_decisions", "limit"),
    ("query_actions", {}, "src.store.query:query_action_items", "limit"),
    ("stale_threads", {}, "src.store.query:find_stale_threads", "limit"),
    ("search_attachments", {"query": "okapi"}, "src.store.query:search_attachments", "limit"),
    (
        "search_conversations",
        {"query": "okapi"},
        "src.store.conversation_query:search_conversations_keyword",
        "limit",
    ),
    (
        "search_conversations",
        {"query": "okapi", "search_type": "semantic"},
        "src.store.embeddings:query_semantic",
        "limit",
    ),
    (
        "recall_preference",
        {"topic": "okapi"},
        "src.store.conversation_query:recall_preferences",
        "limit",
    ),
    (
        "recent_conversations",
        {},
        "src.store.conversation_query:recent_conversations",
        "limit",
    ),
    ("attachment_image_search", {"query": "okapi"}, "src.store.recall:_folded_bucket", 3),
    ("search_teams", {"query": "okapi"}, "src.store.teams_query:search_teams", "limit"),
]


@pytest.mark.parametrize("given, expected", [(-1, 1), (0, 1), (10_000, 200)])
@pytest.mark.parametrize(
    "handler, args, target, arg", CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)]
)
def test_every_limit_is_clamped_to_1_to_200(
    db, monkeypatch, handler, args, target, arg, given, expected
):
    module_name, function = target.split(":")
    module = importlib.import_module(module_name)
    seen: list = []

    def spy(*a, **kw):
        seen.append(kw[arg] if isinstance(arg, str) else a[arg])
        return {} if function in ("get_person_context", "get_topic_context", "recall") else []

    monkeypatch.setattr(module, function, spy)
    limit_name = "limit_per_kind" if handler == "recall" else "limit"

    getattr(mcp_server, handler)(**args, **{limit_name: given})

    assert seen and all(value == expected for value in seen)


def test_the_limit_helper_covers_every_handler_that_takes_one():
    import inspect

    takes_limit = {
        name
        for name, fn in vars(mcp_server).items()
        if inspect.isfunction(fn)
        and not name.startswith("_")
        and fn.__module__ == mcp_server.__name__
        and {"limit", "limit_per_kind"} & set(inspect.signature(fn).parameters)
    }

    # query_calendar_events runs its SQL inline, so no store function can be spied on;
    # its clamp (_cap) is tested in test_audit_calendar_utc.py.
    assert takes_limit - {"query_calendar_events"} == {c[0] for c in CASES}


@pytest.mark.parametrize("limit", [-1, 0])
def test_a_negative_or_zero_limit_returns_one_row_not_all_or_none(db, limit):
    assert len(mcp_server.search_emails("okapi", limit=limit)) == 1
    assert len(mcp_server.search_attachments("okapi", limit=limit)) == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda: mcp_server.search_emails("okapi", search_type="Semantic"),
        lambda: mcp_server.search_conversations("okapi", search_type="fuzzy"),
    ],
)
def test_an_unknown_search_type_is_an_error_naming_the_allowed_values(db, call):
    out = call()

    assert "keyword" in out["error"] and "semantic" in out["error"]


def test_an_unknown_teams_kind_is_an_error_naming_the_allowed_values(db):
    out = mcp_server.search_teams("okapi", kind="bogus")

    assert all(k in out["error"] for k in ("thread", "message", "both"))


@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "01/09/2026"},
        {"end_date": "2026-9-1"},
        {"start_date": "20260901"},
        {"end_date": "2026-13-01"},
        {"start_date": "yesterday"},
        {"start_date": "2026-09-01T10"},
        {"end_date": "2026-09-01T10:00:00+0300"},
        {"start_date": "2026-09-01T1000"},
    ],
)
def test_query_emails_rejects_a_date_that_is_not_iso(db, dates):
    out = mcp_server.query_emails(keyword="okapi", **dates)

    assert isinstance(out, dict)
    assert "YYYY-MM-DD" in out["error"]


@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "2026-09-01"},
        {"start_date": "2026-09-01T00:00:00Z", "end_date": "2026-09-30T23:59:59"},
        {"start_date": "", "end_date": None},
    ],
)
def test_query_emails_accepts_iso_dates_and_empty_ones(db, dates):
    out = mcp_server.query_emails(keyword="okapi", **dates)

    assert isinstance(out, list) and out

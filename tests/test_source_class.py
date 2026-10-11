"""emails.source_class: news and the owner's automation mail, left out of every read path.

On one corpus 16% of all decisions came from automation mail and 14% from news, and only the
decision and action queries left news out. Every emails row now has a class (schema v33, rules
in src/store/source_class.py), set when it is stored, and every read path leaves out news and
automation unless the caller passes include_news or include_automation. Documents and session
notes stay in, and every row that comes back says its class.
"""

import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest

from src.store import source_class as sc
from src.store.schema import create_database, get_connection, get_schema_version, run_migrations

OWNER = "owner@example.com"
COLLEAGUE = "colleague@example.com"
WORD = "zebrafish"
DEFAULT = {"mail", "document", "session_note"}


def _ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _day(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


# --- the rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mailbox", "sender", "subject", "recipients", "pattern", "expected"),
    [
        # News and documents go by mailbox, whoever sent them
        ("News", "digest@news.example", "Anything at all", [], OWNER, "news"),
        ("External", "session-note@documents.local", "[Session note] a.md", [], "", "session_note"),
        (
            "External",
            " Session-Note@Documents.Local ",
            "[Session note] a.md",
            [],
            "",
            "session_note",
        ),
        ("External", "external@documents.local", "[Document] handbook", [], OWNER, "document"),
        (
            "External",
            "sharepoint-page@documents.local",
            "[SharePoint page] Home",
            [],
            "",
            "document",
        ),
        # The automation tags, from anyone, at the start of the subject only
        ("Inbox", "ops@example.net", "[VPS] nightly backup", [COLLEAGUE], "", "automation"),
        ("Archive", "ops@example.net", "  [alert] disk almost full", [OWNER], "", "automation"),
        ("Inbox", COLLEAGUE, "Re: [VPS] nightly backup", [OWNER], OWNER, "mail"),
        ("Inbox", COLLEAGUE, "Budget [VPS] figures", [OWNER], OWNER, "mail"),
        # The owner's mail to himself alone, stamped by a job
        ("Sent Items", OWNER, "[nightly] health: 3 warnings", [OWNER], OWNER, "automation"),
        ("Sent Items", OWNER, "health report 2026-05-28", [OWNER], OWNER, "automation"),
        (
            "Archive",
            "Owner@Example.com",
            "[digest] week 21",
            ["OWNER@example.com"],
            OWNER,
            "automation",
        ),
        # ... and what stays mail: when unsure, mail
        ("Sent Items", OWNER, "notes for the Monday call", [OWNER], OWNER, "mail"),
        ("Sent Items", OWNER, "Re: health report 2026-05-28", [OWNER], OWNER, "mail"),
        ("Sent Items", OWNER, "FW: [nightly] health", [OWNER], OWNER, "mail"),
        ("Sent Items", OWNER, "Απ: αναφορά 2026-05-28", [OWNER], OWNER, "mail"),
        ("Sent Items", OWNER, "health report 2026-05-28", [OWNER, COLLEAGUE], OWNER, "mail"),
        ("Sent Items", OWNER, "health report 2026-05-28", [], OWNER, "mail"),
        ("Inbox", COLLEAGUE, "health report 2026-05-28", [OWNER], OWNER, "mail"),
        ("Sent Items", OWNER, "health report 2026-05-28", [OWNER], "", "mail"),
        ("Sent Items", OWNER, "build 20260528 notes", [OWNER], OWNER, "mail"),
        ("Sent Items", OWNER, "[two words] in a bracket", [OWNER], OWNER, "mail"),
        (None, None, None, [], OWNER, "mail"),
    ],
)
def test_the_rules(mailbox, sender, subject, recipients, pattern, expected):
    assert sc.classify(mailbox, sender, subject, recipients, owner_pattern=pattern) == expected


def test_the_owner_is_the_configured_one_by_default(monkeypatch):
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    assert sc.classify("Sent Items", OWNER, "report 2026-05-28", [OWNER]) == "automation"
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "")
    assert sc.classify("Sent Items", OWNER, "report 2026-05-28", [OWNER]) == "mail"


def test_the_hidden_classes():
    assert sc.hidden_classes() == ("news", "automation")
    assert sc.hidden_classes(include_news=True) == ("automation",)
    assert sc.hidden_classes(include_automation=True) == ("news",)
    assert sc.hidden_classes(include_news=True, include_automation=True) == ()


# --- a store with one row of each class -----------------------------------------------------


def _metadata(message_id, subject, sender, *, mailbox="Inbox", to=(OWNER,), days_ago=10):
    def person(address):
        return {"name": address.split("@")[0].title(), "address": address}

    return {
        "message_id": message_id,
        "date_received": _ago(days_ago),
        "sender": person(sender),
        "subject": subject,
        "mailbox_name": mailbox,
        "content": f"The {WORD} body of message {message_id}",
        "to_recipients": [person(a) for a in to],
        "cc_recipients": [],
        "conversation_id": f"conv-{message_id}",
    }


def _extraction(label):
    return {
        "summary": f"A {WORD} summary of the {label}",
        "topics": [f"{WORD} pilot"],
        "decisions": [{"decision": f"{label} {WORD} decision", "decided_by": "Someone"}],
        "action_items": [
            {"task": f"{label} {WORD} action", "owner": "Someone", "deadline": _day(-5)}
        ],
        "commitments": [{"commitment": f"{label} {WORD} commitment", "by": "A", "to": "B"}],
        "key_facts": [f"{label} {WORD} fact"],
        "people_roles": {"Colleague": "mentioned"},
    }


# message id -> (subject, sender, mailbox, label, class); loaded through the loader
LOADED = {
    1: (f"{WORD} pilot budget", COLLEAGUE, "Inbox", "mail", "mail"),
    2: (f"[News/digest] {WORD} market wrap", "digest@news.example", "News", "news", "news"),
    3: (f"[VPS] {WORD} nightly report", OWNER, "Sent Items", "tagged", "automation"),
    4: (f"{WORD} health {_day(-1)}", OWNER, "Sent Items", "stamped", "automation"),
}


def _attachment(conn, email_id, text):
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (?, ?, 'report.pdf', 'application/pdf', 1, ?, ?)",
        (email_id, email_id, f"/data/attachments/{email_id}/report.pdf", _ago(1)),
    ).lastrowid
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, summary, llm_status) VALUES (?, ?, 'pymupdf',"
        " 'extracted', ?, ?, 'extracted')",
        (att, text, _ago(1), f"Summary: {text}"),
    )
    return att


def _document(conn, source, subject, sha):
    from src.extract.attachment_pipeline import ingest_text_document

    out = ingest_text_document(
        conn,
        source=source,
        key=subject,
        filename=f"{WORD}.md",
        mime_type="text/markdown",
        text=f"The {WORD} text of {subject}",
        sha256=sha,
        method="session-note",
        status="extracted",
        error=None,
        subject=subject,
        sender_name="Someone",
        date=_ago(8),
    )
    return out["email_id"]


def _build(path, monkeypatch) -> dict:
    """The store; returns email id -> expected class."""
    from src.store.loader import load_single_email
    from src.store.normalizer import find_or_create_topic

    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    create_database(str(path)).close()
    conn = get_connection(str(path))
    expected = {}
    for message_id, (subject, sender, mailbox, label, cls) in LOADED.items():
        assert load_single_email(
            conn, _metadata(message_id, subject, sender, mailbox=mailbox), _extraction(label)
        )
        conn.commit()
        email_id = conn.execute(
            "SELECT id FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()[0]
        expected[email_id] = cls
    by_label = {LOADED[m][3]: e for m, e in zip(LOADED, expected, strict=True)}
    _attachment(conn, by_label["mail"], f"mail attachment on {WORD}")
    _attachment(conn, by_label["tagged"], f"report attachment on {WORD}")

    colleague = conn.execute("SELECT id FROM people WHERE email = ?", (COLLEAGUE,)).fetchone()[0]
    topic = find_or_create_topic(conn, f"{WORD} pilot")
    for source, subject, sha, cls in (
        ("sharepoint", f"[SharePoint] {WORD} handbook", "a" * 64, "document"),
        ("session-note", f"[Session note] /notes/{WORD}.md", "b" * 64, "session_note"),
    ):
        email_id = _document(conn, source, subject, sha)
        expected[email_id] = cls
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'author')",
            (email_id, colleague),
        )
        conn.execute(
            "INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)", (email_id, topic)
        )
        conn.execute(
            "INSERT INTO decisions (email_id, decision) VALUES (?, ?)",
            (email_id, f"{cls} {WORD} decision"),
        )
        conn.execute(
            "INSERT INTO action_items (email_id, task, deadline, status) VALUES (?, ?, ?, 'open')",
            (email_id, f"{cls} {WORD} action", _day(-5)),
        )
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (email_id, f"{cls} {WORD} fact")
        )
    # An item with no email at all: never filtered, and of no class
    conn.execute(
        "INSERT INTO teams_chats (id, teams_chat_id, chat_kind, first_seen_at)"
        " VALUES (1, 'C1', 'group', ?)",
        (_ago(9),),
    )
    conn.execute(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, title, started_at, ended_at)"
        " VALUES (1, 1, 'chat_session', 'A thread', ?, ?)",
        (_ago(9), _ago(9)),
    )
    conn.execute(
        "INSERT INTO decisions (teams_thread_id, decision) VALUES (1, ?)",
        (f"teams {WORD} decision",),
    )
    conn.execute(
        "INSERT INTO action_items (teams_thread_id, task, deadline, status) VALUES (1, ?, ?, 'open')",
        (f"teams {WORD} action", _day(-5)),
    )
    conn.commit()
    conn.close()
    return expected


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    expected = _build(path, monkeypatch)
    conn = get_connection(str(path))
    yield conn, expected, path
    conn.close()


def _classes(rows) -> set:
    return {row["source_class"] for row in rows}


def _switched(call) -> dict:
    """The classes a read path returns by default and with each switch."""
    return {
        "default": _classes(call()),
        "news": _classes(call(include_news=True)),
        "automation": _classes(call(include_automation=True)),
        "both": _classes(call(include_news=True, include_automation=True)),
    }


def _expect(default) -> dict:
    """What _switched gives for a path whose default view is `default`."""
    return {
        "default": default,
        "news": default | {"news"},
        "automation": default | {"automation"},
        "both": default | {"news", "automation"},
    }


# --- storing it -----------------------------------------------------------------------------


def test_each_row_is_stored_with_its_class(store):
    conn, expected, _ = store
    stored = dict(conn.execute("SELECT id, source_class FROM emails").fetchall())
    assert stored == expected


def test_a_stored_document_file_is_a_document(tmp_path):
    from src.extract.attachment_pipeline import ingest_document

    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    doc = tmp_path / "handbook.txt"
    doc.write_text(f"The {WORD} handbook", encoding="utf-8")
    out = ingest_document(str(doc), db_path=str(path))
    conn = get_connection(str(path))
    row = conn.execute(
        "SELECT source_class FROM emails WHERE id = ?", (out["email_id"],)
    ).fetchone()
    conn.close()
    assert row[0] == "document"


def test_a_writer_on_a_store_from_before_v33_still_stores(tmp_path):
    """A writer can reach a store before anything migrated it: it stores what it always did."""
    from src.extract.attachment_pipeline import ingest_text_document
    from src.store.loader import load_single_email

    conn = _as_v32(tmp_path / "old.db")
    assert load_single_email(conn, _metadata(9, "plain mail", COLLEAGUE), _extraction("old"))
    ingest_text_document(
        conn,
        source="sharepoint",
        key="k",
        filename="f.md",
        mime_type="text/markdown",
        text="t",
        sha256="c" * 64,
        method=None,
        status="extracted",
        error=None,
        subject="[SharePoint] f",
        sender_name="SharePoint",
        date=_ago(1),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 2
    conn.close()


# --- the migration --------------------------------------------------------------------------


def _as_v32(path):
    """A store as v32 left it: no source_class column, no index, stamped 32."""
    create_database(str(path)).close()
    conn = get_connection(str(path))
    conn.execute("DROP INDEX idx_emails_source_class")
    conn.execute("ALTER TABLE emails DROP COLUMN source_class")
    conn.execute("UPDATE schema_version SET version = 32")
    conn.commit()
    return conn


def _seed_v32(conn):
    """One row of each class, written as v32 code wrote them; returns id -> expected class."""
    rows = [
        (1, "Inbox", COLLEAGUE, f"{WORD} pilot budget", "mail"),
        (2, "News", "digest@news.example", f"{WORD} market wrap", "news"),
        (3, "Sent Items", OWNER, f"[VPS] {WORD} nightly report", "automation"),
        (4, "Sent Items", OWNER, f"{WORD} health 2026-05-28", "automation"),
        (5, "Sent Items", OWNER, f"{WORD} notes to self", "mail"),
        (6, "External", "external@documents.local", f"[Document] {WORD}", "document"),
        (
            7,
            "External",
            "session-note@documents.local",
            f"[Session note] {WORD}.md",
            "session_note",
        ),
    ]
    conn.execute("INSERT INTO people (id, name, email) VALUES (1, 'Owner', ?)", (OWNER,))
    conn.execute("INSERT INTO people (id, name, email) VALUES (2, 'Colleague', ?)", (COLLEAGUE,))
    for email_id, mailbox, sender, subject, _ in rows:
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, sender_address, subject, summary,"
            " mailbox_name, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (email_id, email_id, _ago(3), sender, subject, f"{WORD} summary", mailbox, "body text"),
        )
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, 1, 'recipient')",
            (email_id,),
        )
    conn.commit()
    return {email_id: cls for email_id, *_, cls in rows}


def _au(conn):
    return conn.execute("SELECT sql FROM sqlite_master WHERE name = 'emails_au'").fetchone()[0]


def test_the_migration_classifies_a_v32_store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    conn = _as_v32(tmp_path / "brain.db")
    expected = _seed_v32(conn)
    trigger = _au(conn)

    run_migrations(conn)

    assert get_schema_version(conn) == 33
    assert dict(conn.execute("SELECT id, source_class FROM emails").fetchall()) == expected
    assert "idx_emails_source_class" in {r[1] for r in conn.execute("PRAGMA index_list(emails)")}
    # The full-text trigger was set aside for the backfill and is back as it was
    assert _au(conn) == trigger
    conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('integrity-check')")
    matched = {
        r[0] for r in conn.execute("SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?", (WORD,))
    }
    assert matched == set(expected)
    # ... and it still keeps the index in step
    conn.execute("UPDATE emails SET summary = 'a quokka appeared' WHERE id = 1")
    assert [
        r[0] for r in conn.execute("SELECT rowid FROM emails_fts WHERE emails_fts MATCH 'quokka'")
    ] == [1]
    conn.close()


def test_the_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    conn = _as_v32(tmp_path / "brain.db")
    expected = _seed_v32(conn)
    run_migrations(conn)
    from src.store.schema import migrate_add_source_class

    migrate_add_source_class(conn)
    run_migrations(conn)

    assert get_schema_version(conn) == 33
    assert dict(conn.execute("SELECT id, source_class FROM emails").fetchall()) == expected
    conn.close()


def test_the_migration_reads_a_store_older_than_its_columns(tmp_path, monkeypatch):
    """A store from before senders, mailboxes and people were recorded still migrates: what it
    has is classified, and what it lacks reads as nothing."""
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE emails (id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE NOT NULL,"
        " date_received TEXT NOT NULL, subject TEXT)"
    )
    conn.executemany(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, '2025-01-01', ?)",
        [(1, "Budget"), (2, "[VPS] nightly backup")],
    )
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (32)")
    conn.commit()
    conn.close()
    conn = get_connection(str(path))

    run_migrations(conn)

    assert dict(conn.execute("SELECT message_id, source_class FROM emails").fetchall()) == {
        1: "mail",
        2: "automation",
    }
    conn.close()


def test_the_migration_rolls_back_whole(tmp_path, monkeypatch):
    """A failure in the backfill leaves the store as it was: no column, the trigger in place."""
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    conn = _as_v32(tmp_path / "brain.db")
    _seed_v32(conn)
    trigger = _au(conn)
    conn.execute(
        "CREATE TRIGGER refuse BEFORE UPDATE ON emails BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        run_migrations(conn)

    assert get_schema_version(conn) == 32
    assert "source_class" not in {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    assert _au(conn) == trigger
    conn.close()


def test_without_the_owner_pattern_his_reports_stay_mail_until_classified(
    tmp_path, monkeypatch, capsys
):
    import types

    from src import cli

    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "")
    path = tmp_path / "brain.db"
    conn = _as_v32(path)
    expected = _seed_v32(conn)
    run_migrations(conn)
    stored = dict(conn.execute("SELECT id, source_class FROM emails").fetchall())
    conn.close()
    assert stored == {**expected, 4: "mail"}
    assert "BRAIN_USER_EMAIL_PATTERN is unset" in capsys.readouterr().err

    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    cli.cmd_classify_sources(types.SimpleNamespace(db=path))

    conn = get_connection(str(path))
    assert dict(conn.execute("SELECT id, source_class FROM emails").fetchall()) == expected
    conn.close()
    assert "automation: 2" in capsys.readouterr().out


# --- reading it: every path leaves out news and automation by default -----------------------


def test_keyword_search(store):
    from src.store.query import query_by_keyword

    conn, _, _ = store
    seen = _switched(lambda **kw: query_by_keyword(conn, WORD, limit=50, **kw))
    assert seen == _expect(DEFAULT)


def test_a_thread_asked_for_by_id_comes_back_whole_and_says_its_class(store):
    """email_thread reads the thread it is given, whatever its class, and says what it is."""
    from src.store.query import query_thread

    conn, expected, _ = store
    for email_id, cls in expected.items():
        assert [(r["email_id"], r["source_class"]) for r in query_thread(conn, email_id)] == [
            (email_id, cls)
        ]


def test_person_topic_date_and_combined_queries(store):
    from src.store.query import query_by_date_range, query_by_person, query_by_topic, query_combined

    conn, _, _ = store
    start, end = _day(-30), _day(1)
    calls = [
        lambda **kw: query_by_person(conn, COLLEAGUE, limit=50, **kw),
        lambda **kw: query_by_person(conn, "Colleague", limit=50, **kw),
        lambda **kw: query_by_topic(conn, WORD, limit=50, **kw),
        lambda **kw: query_by_date_range(conn, start, end, limit=50, **kw),
        lambda **kw: query_combined(conn, keyword=WORD, limit=50, **kw),
        lambda **kw: query_combined(conn, person="Colleague", start_date=start, limit=50, **kw),
    ]
    for call in calls:
        assert _switched(call) == _expect(DEFAULT)


def test_decisions_and_actions(store):
    from src.store.query import (
        count_overdue_actions,
        find_overdue_actions,
        query_action_items,
        query_decisions,
    )

    conn, _, _ = store
    with_teams = DEFAULT | {None}  # an item with no email passes, and has no class
    assert _switched(lambda **kw: query_decisions(conn, limit=50, **kw)) == _expect(with_teams)
    assert _switched(lambda **kw: query_action_items(conn, limit=50, **kw)) == _expect(with_teams)
    assert _switched(lambda **kw: find_overdue_actions(conn, limit=50, **kw)) == _expect(with_teams)
    assert count_overdue_actions(conn) == 4  # mail, document, session note, Teams
    assert count_overdue_actions(conn, include_news=True, include_automation=True) == 7


def test_the_dossiers(store):
    from src.store.context import get_person_context, get_topic_context

    conn, _, _ = store
    for name in ("Colleague", COLLEAGUE):
        ctx = get_person_context(conn, name)
        assert _classes(ctx["recent_emails"]) == DEFAULT
        assert _classes(ctx["decisions"]) == DEFAULT
        assert _classes(ctx["open_actions"]) == DEFAULT
        assert ctx["email_count"] == ctx["decisions_total"] == ctx["open_actions_total"] == 3
        full = get_person_context(conn, name, include_news=True, include_automation=True)
        assert _classes(full["decisions"]) == DEFAULT | {"news", "automation"}
        assert full["email_count"] == 6

    ctx = get_topic_context(conn, f"{WORD} pilot")
    for key in ("recent_emails", "decisions", "open_actions", "key_facts"):
        assert _classes(ctx[key]) == DEFAULT, key
    assert ctx["email_count"] == ctx["key_facts_total"] == 3
    full = get_topic_context(conn, f"{WORD} pilot", include_news=True, include_automation=True)
    assert _classes(full["key_facts"]) == DEFAULT | {"news", "automation"}
    assert full["email_count"] == 6


def test_meeting_prep(store):
    from src.store.query import meeting_prep

    conn, _, _ = store
    prep = meeting_prep(conn, ["Colleague"], topic=WORD)
    attendee, topic = prep["attendees"][0], prep["topic_context"]
    for rows in (attendee["emails"], attendee["decisions"], attendee["open_actions"]):
        assert _classes(rows) == DEFAULT
    for rows in (topic["decisions"], topic["key_facts"], topic["action_items"]):
        assert _classes(rows) == DEFAULT
    full = meeting_prep(conn, ["Colleague"], topic=WORD, include_automation=True)
    assert _classes(full["attendees"][0]["emails"]) == DEFAULT | {"automation"}
    assert _classes(full["topic_context"]["key_facts"]) == DEFAULT | {"automation"}


def test_attachment_search(store):
    from src.store.query import search_attachments

    conn, _, _ = store
    # The news item has no attachment, so only the automation switch changes anything here
    assert _switched(lambda **kw: search_attachments(conn, WORD, limit=50, **kw)) == {
        "default": DEFAULT,
        "news": DEFAULT,
        "automation": DEFAULT | {"automation"},
        "both": DEFAULT | {"automation"},
    }


def test_stale_threads(store, monkeypatch):
    from src.store import query
    from src.store.loader import load_single_email

    conn, _, _ = store
    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", OWNER)
    for message_id, subject in ((7, f"[VPS] {WORD} backup"), (8, f"{WORD} follow-up")):
        load_single_email(
            conn,
            _metadata(message_id, subject, OWNER, mailbox="Sent Items", to=(COLLEAGUE,)),
            _extraction("stale"),
        )
    conn.commit()

    rows = query.find_stale_threads(conn, days=5)
    assert [r["subject"] for r in rows] == [f"{WORD} follow-up"]
    assert _classes(rows) == {"mail"}
    assert query.count_stale_threads(conn, days=5) == 1
    assert query.count_stale_threads(conn, days=5, include_automation=True) == 2


def test_recall(store):
    from src.store.recall import recall

    conn, _, _ = store
    out = recall(conn, WORD, limit_per_kind=20)
    full = recall(conn, WORD, limit_per_kind=20, include_news=True, include_automation=True)
    for kind in ("emails", "decisions", "actions", "commitments"):
        assert _classes(out[kind]) <= DEFAULT | {None}, kind
        assert {"news", "automation"} <= _classes(full[kind]), kind
    assert _classes(out["attachments"]) == DEFAULT
    assert "automation" in _classes(full["attachments"])
    assert _classes(out["topic_context"]["key_facts"]) == DEFAULT
    assert {"news", "automation"} <= _classes(full["topic_context"]["key_facts"])


def test_recall_drops_hidden_semantic_candidates(store):
    from src.store.recall import recall

    conn, expected, _ = store
    hidden = [i for i, cls in expected.items() if cls in ("news", "automation")]

    out = recall(conn, "unrelated words", semantic_candidates=lambda c, q, n: hidden)
    assert out["emails"] == []
    shown = recall(
        conn,
        "unrelated words",
        semantic_candidates=lambda c, q, n: hidden,
        include_news=True,
        include_automation=True,
    )
    assert {r["email_id"] for r in shown["emails"]} == set(hidden)
    assert _classes(shown["emails"]) == {"news", "automation"}


def _index(conn, path):
    """An embedding index where every email and attachment points the same way."""
    from src.store import embeddings

    ids = [r[0] for r in conn.execute("SELECT id FROM emails")]
    ids += [-r[0] for r in conn.execute("SELECT id FROM attachment_content")]
    np.savez(path, ids=np.array(ids, dtype=np.int64), vectors=np.ones((len(ids), 4), np.float32))
    embeddings._INDEX_CACHE.update({"path": None, "mtime": None, "ids": None, "unit": None})
    return path


def _embed(texts):
    return np.ones((len(texts), 4), dtype=np.float32)


def test_semantic_search(store, tmp_path):
    from src.store.embeddings import query_semantic, semantic_email_candidates

    conn, expected, _ = store
    index = _index(conn, tmp_path / "embeddings.npz")
    hidden = {i for i, cls in expected.items() if cls in ("news", "automation")}

    def semantic(**kw):
        return query_semantic(conn, WORD, limit=50, embed_fn=_embed, index_path=index, **kw)

    rows = semantic()
    assert hidden.isdisjoint(r["email_id"] for r in rows if r["type"] == "email")
    assert _classes(rows) == DEFAULT  # attachment rows say their email's class
    assert _classes(semantic(include_news=True, include_automation=True)) == DEFAULT | {
        "news",
        "automation",
    }

    def candidates(**kw):
        return semantic_email_candidates(conn, WORD, 50, embed_fn=_embed, index_path=index, **kw)

    assert set(candidates()) == set(expected) - hidden
    assert set(candidates(include_news=True, include_automation=True)) == set(expected)


def test_a_store_from_before_v33_is_read_by_the_same_rules(store):
    """A replica runs new code on the store it last pulled: news and tagged automation are
    still left out, read off the rows; only the owner's stamped mail to himself is not."""
    from src.store.query import query_by_keyword, query_decisions

    conn, _, _ = store
    conn.execute("DROP INDEX idx_emails_source_class")
    conn.execute("ALTER TABLE emails DROP COLUMN source_class")
    conn.commit()

    rows = query_by_keyword(conn, WORD, limit=50)
    assert _classes(rows) == DEFAULT
    assert len(rows) == 4  # the stamped report reads as mail without its column
    full = query_by_keyword(conn, WORD, limit=50, include_news=True, include_automation=True)
    assert _classes(full) == DEFAULT | {"news", "automation"}
    assert "news" not in _classes(query_decisions(conn, limit=50))


# --- the MCP tools pass the switches through ------------------------------------------------


def test_the_tools_take_the_switches(store, monkeypatch):
    from src import mcp_server

    conn, _, path = store
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))
    on = {"include_news": True, "include_automation": True}
    tools = [
        lambda **kw: mcp_server.search_emails(WORD, **kw),
        lambda **kw: mcp_server.query_emails(keyword=WORD, **kw),
        lambda **kw: mcp_server.query_decisions(**kw),
        lambda **kw: mcp_server.query_actions(**kw),
        lambda **kw: mcp_server.search_attachments(WORD, **kw),
        lambda **kw: mcp_server.recall(WORD, **kw)["decisions"],
        lambda **kw: mcp_server.person_context("Colleague", **kw)["decisions"],
        lambda **kw: mcp_server.topic_context(f"{WORD} pilot", **kw)["key_facts"],
        lambda **kw: mcp_server.meeting_prep("Colleague", **kw)["attendees"][0]["emails"],
        lambda **kw: mcp_server.stale_threads(days=1, **kw)["overdue_actions"],
    ]
    for tool in tools:
        assert not {"news", "automation"} & _classes(tool())
        assert "automation" in _classes(tool(**on))

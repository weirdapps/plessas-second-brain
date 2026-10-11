"""Every migration runs again over its own result, from every version on.

run_migrations stamps the new version only after its last step. A run that fails
or is stopped part way leaves the old version in place, with the steps before the
failure done, and the next start runs every step from that version again, the
finished ones included (code-tests-ci-08). create_database runs the chain once,
from zero, so it shows only that each step works on the store as the steps before
it left it, not that it can run a second time.

So for each version a store can be migrated from, a store at the current schema,
holding a row of every kind the tools read, is stamped back to that version and
migrated, twice, which is exactly the state such a failure leaves. The schema
(every table and its columns, every index, trigger and view) must come out as a
new store's, the rows as they went in, and every full-text index whole.
"""

import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    create_database,
    get_connection,
    get_schema_version,
    run_migrations,
    set_schema_version,
)
from tests import factories as f

# A word in a row of every table with a full-text index, found after every run.
WORD = "okapi"


def _virtual_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL%'"
        )
    ]


def _schema(conn: sqlite3.Connection) -> dict:
    """Each object's normalised SQL, and each table's columns, generated ones included."""
    objects = {
        (kind, name): " ".join((sql or "").split())
        for kind, name, sql in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {
        name: [tuple(column) for column in conn.execute(f"PRAGMA table_xinfo('{name}')")]
        for kind, name in objects
        if kind == "table"
    }
    return {"objects": objects, "columns": columns}


def _rows(conn: sqlite3.Connection) -> dict:
    """Every row of every table, the full-text indexes' own tables aside: a rebuild may
    lay an index out differently, and _assert_indexes_whole checks those."""
    indexes = _virtual_tables(conn)
    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        if not any(name == fts or name.startswith(f"{fts}_") for fts in indexes)
    ]
    return {
        name: sorted(map(tuple, conn.execute(f"SELECT * FROM '{name}'")), key=repr)
        for name in tables
    }


def _assert_indexes_whole(conn: sqlite3.Connection) -> None:
    for fts in _virtual_tables(conn):
        # FTS5's own check, of the index against the table it indexes; raises when they differ.
        conn.execute(f"INSERT INTO {fts}({fts}) VALUES('integrity-check')")
        assert _finds(conn, fts), f"{fts} no longer finds what it holds"


def _seed(conn: sqlite3.Connection) -> None:
    """A row of every kind, as the loaders write them, each saying 'okapi'."""
    email_id = f.email(
        conn,
        subject="Okapi plan",
        content="the okapi figures",
        cc=[("Carol White", "carol@example.com")],
        topics=["okapi"],
    )
    f.email(conn, subject="Re: Okapi plan", sender=f.BOB, to=[f.ALICE], content="okapi reply")
    attachment_id = f.attachment(
        conn, email_id=email_id, text="the okapi budget", summary="Okapi budget"
    )
    thread_id = f.teams_thread(conn, title="Okapi thread", summary="The okapi thread")
    f.teams_message(conn, thread_id=thread_id, content_text="the okapi numbers")
    event_id = f.calendar_event(conn, subject="Okapi review", body_summary="the okapi review")
    f.decision(conn, "Adopt the okapi plan", email_id=email_id)
    f.decision(conn, "Pilot the okapi tracker", teams_thread_id=thread_id)
    f.action_item(conn, "Draft the okapi budget", email_id=email_id)
    f.action_item(conn, "Book the okapi room", event_id=event_id)
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi costs 5')", (email_id,))
    conn.execute(
        "INSERT INTO key_facts (attachment_id, fact) VALUES (?, 'okapi budget is 9')",
        (attachment_id,),
    )
    # The kinds tests/factories.py does not write, so that every full-text index holds a row.
    started = f.ago(days=2)
    conn.executescript(
        f"""
        INSERT INTO conversations (id, session_id, started_at, ended_at, project_name,
                                   turn_count, summary, created_at)
            VALUES (1, 's1', '{started}', '{started}', 'okapi', 1, 'An okapi session',
                    '{started}');
        INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,
                                        content)
            VALUES (1, 0, '{started}', 'user', 'the okapi question');
        INSERT INTO whatsapp_chats (id, chat_jid, name, chat_kind, first_seen_at)
            VALUES (1, '120000000000000001@g.us', 'Chat A', 'group', '{started}');
        INSERT INTO whatsapp_threads (id, chat_id, anchor_message_id, started_at, ended_at,
                                      message_count, participant_names, title, summary)
            VALUES (1, 1, 'w1', '{started}', '{started}', 1, '["Carol Example"]',
                    'Okapi chat', 'Planning the okapi visit');
        INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, thread_id, sender_jid,
                                       sender_name, is_from_me, sent_at, content)
            VALUES ('120000000000000001@g.us', 'w1', 1, 1, '30000000001@s.whatsapp.net',
                    'Carol Example', 0, '{started}', 'okapi tickets bought');
        """
    )
    conn.commit()


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    """A new store's schema, and a seeded store at the current version to copy."""
    root = tmp_path_factory.mktemp("migrations")
    fresh = create_database(str(root / "fresh.db"))
    schema = _schema(fresh)
    fresh.close()
    seeded = create_database(str(root / "seeded.db"))
    _seed(seeded)
    rows = _rows(seeded)
    seeded.close()
    return {"schema": schema, "rows": rows, "seeded": root / "seeded.db"}


def _copy(source, target) -> sqlite3.Connection:
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    src.close()
    dst.close()
    return get_connection(str(target))


def _finds(conn: sqlite3.Connection, fts: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {fts} WHERE {fts} MATCH ?", (WORD,)).fetchone()[0]


def test_the_seed_holds_the_word_in_every_full_text_index(reference, tmp_path):
    """Otherwise _assert_indexes_whole has nothing to find in an index left empty."""
    conn = _copy(reference["seeded"], tmp_path / "brain.db")

    assert [fts for fts in _virtual_tables(conn) if not _finds(conn, fts)] == []


@pytest.mark.parametrize("version", range(CURRENT_SCHEMA_VERSION))
def test_migrating_again_from_a_version_changes_nothing(reference, tmp_path, version):
    conn = _copy(reference["seeded"], tmp_path / "brain.db")

    for _ in range(2):
        set_schema_version(conn, version)
        run_migrations(conn)

    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert _schema(conn) == reference["schema"]
    assert _rows(conn) == reference["rows"]
    _assert_indexes_whole(conn)
    conn.close()

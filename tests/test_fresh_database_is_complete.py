"""A database built by create_database() must be usable by everything.

create_database() stamped schema_version = CURRENT_SCHEMA_VERSION while creating
only the base tables: Teams (v12), calendar (v13) and conversations (v7) are
added by migrations, and the stamp told run_migrations there was nothing to do.
A fresh or restored database therefore came up with 32 tables instead of 67,
missing calendar_events, event_attendees, teams_chats, teams_messages,
teams_threads, teams_mri_resolution, conversations, conversation_turns and
conversation_topics, while `migrate` reported it up to date. That is the
disaster-recovery path, so the failure mode is "rebuild the brain from a backup
and silently lose Teams, calendar and conversation memory".

The existing schema tests hand-applied the migrations, which is exactly why they
stayed green: they tested a database production never builds.
"""

import inspect
import sqlite3

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import create_database, get_schema_version

# Added by migrations, absent from the base DDL. This is the actual regression.
MIGRATION_TABLES = [
    "conversations",
    "conversation_turns",
    "conversation_topics",
    "teams_chats",
    "teams_threads",
    "teams_messages",
    "teams_mri_resolution",
    "calendar_events",
    "event_attendees",
    "sharepoint_links",
    "inline_images",
    "inline_image_occurrences",
]


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_is_stamped_at_current_version(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    conn.close()


def test_fresh_database_has_every_migration_table(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    missing = [t for t in MIGRATION_TABLES if t not in _tables(conn)]
    conn.close()
    assert missing == [], f"fresh database is missing {missing}"


def test_running_migrations_again_is_a_no_op(tmp_path):
    """create_database now calls run_migrations itself, so the CLI's `migrate`
    running straight after must be harmless and must not re-fire anything.
    """
    from src.store.schema import run_migrations

    path = str(tmp_path / "b.db")
    conn = create_database(path)
    before = _tables(conn)
    run_migrations(conn)
    assert _tables(conn) == before
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    conn.close()


def test_every_mcp_tool_runs_against_a_fresh_database(tmp_path, monkeypatch):
    """The end-to-end assertion: no MCP tool may raise "no such table" on a
    database this repo just created. Eight of them did.
    """
    import src.mcp_server as server

    path = str(tmp_path / "b.db")
    create_database(path).close()

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(server, "_get_conn", _conn)

    # Sample values by parameter name; anything unknown gets a harmless string.
    sample = {
        "name_or_email": "nobody@example.com",
        "topic": "anything",
        "query": "anything",
        "people": "nobody@example.com",
        "session_id": "00000000-0000-0000-0000-000000000000",
        "operation": "list_stale",
        "thread_id": 1,
        "chat_id": 1,
    }

    skip = {"outlook_live_search"}  # shells out to outlook-cli; not a DB path
    checked = 0
    for name, fn in vars(server).items():
        if name.startswith("_") or name in skip or not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        if getattr(fn, "__module__", None) != "src.mcp_server":
            continue
        kwargs = {}
        ok = True
        for pname, p in sig.parameters.items():
            # Fill anything we have a sample for, optional or not: query_emails
            # rejects a call with no filter at all, by design.
            if pname in sample:
                kwargs[pname] = sample[pname]
            elif p.default is inspect.Parameter.empty:
                ok = False
        if not ok:
            continue
        fn(**kwargs)  # must not raise
        checked += 1

    assert checked >= 15, f"only exercised {checked} tools; the sweep is not finding them"

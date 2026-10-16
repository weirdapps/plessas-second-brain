"""v37: topic pages and the findings research sessions file back.

A topic page is derived: a model writes it from cited store items, so it lives in a table
of its own, with no full-text index and no vector, and nothing reads it as a source. A
finding is derived too, parsed from a fenced block in a note a Claude session wrote
(src/export/session_notes.py, src/store/findings.py). Both tables are new: the migration
rewrites no row and touches no full-text index, and running it again changes nothing.
"""

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    create_database,
    get_schema_version,
    migrate_add_topic_pages,
    run_migrations,
    set_schema_version,
)
from tests import factories as f

TOPIC_PAGE_COLUMNS = [
    "id",
    "topic",
    "display_name",
    "members",
    "selected_by",
    "body",
    "citations",
    "evidence_keys",
    "built_at",
    "newest_evidence_at",
    "prompt_hash",
    "model",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "lines_kept",
    "lines_dropped",
]
FINDING_COLUMNS = [
    "id",
    "note_path",
    "ordinal",
    "session_id",
    "author_kind",
    "written_at",
    "claim",
    "claim_key",
    "value",
    "as_of",
    "source_ref",
    "source_date",
    "quote",
    "supersedes",
    "status",
    "reason",
    "loaded_at",
]


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def test_a_fresh_store_is_at_v37(store):
    assert CURRENT_SCHEMA_VERSION >= 37
    assert get_schema_version(store) == CURRENT_SCHEMA_VERSION


def test_the_two_tables_have_their_columns(store):
    assert _columns(store, "topic_pages") == TOPIC_PAGE_COLUMNS
    assert _columns(store, "findings") == FINDING_COLUMNS


def test_neither_table_has_a_full_text_index_or_a_trigger(store):
    """Pages and findings are derived: never searched as if they were sources."""
    objects = store.execute(
        "SELECT type, name FROM sqlite_master WHERE name LIKE '%topic_page%' "
        "OR name LIKE '%finding%' OR sql LIKE '%topic_pages%' OR sql LIKE '%findings%'"
    ).fetchall()
    kinds = {(kind, name) for kind, name in objects}

    assert ("table", "topic_pages") in kinds and ("table", "findings") in kinds
    assert not [name for kind, name in kinds if kind == "trigger"]
    assert not [name for kind, name in kinds if name.endswith("_fts")]


def test_a_finding_is_rejected_unless_a_check_accepted_it(store):
    """Fail closed: the column's default, and nothing but the two statuses."""
    store.execute(
        "INSERT INTO findings (note_path, ordinal, claim, claim_key, reason, loaded_at)"
        " VALUES ('/n/a.md', 0, 'c', 'c', 'not checked', '2026-10-16')"
    )
    assert tuple(store.execute("SELECT status, author_kind FROM findings").fetchone()) == (
        "rejected",
        "agent",
    )
    with pytest.raises(Exception, match="CHECK"):
        store.execute("UPDATE findings SET status = 'sourced'")


def test_one_finding_per_note_position(store):
    row = ("/n/a.md", 1, "s1", "2026-10-01", "claim", "claim", "ok", "2026-10-16")
    insert = (
        "INSERT INTO findings (note_path, ordinal, session_id, written_at, claim, claim_key,"
        " reason, loaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    store.execute(insert, row)
    with pytest.raises(Exception, match="UNIQUE"):
        store.execute(insert, row)


def test_migrating_a_v35_store_adds_both_tables_and_keeps_every_row(store):
    email_id = f.email(store, subject="Okapi plan")
    store.execute("DROP TABLE topic_pages")
    store.execute("DROP TABLE findings")
    store.commit()
    set_schema_version(store, 35)

    run_migrations(store)
    run_migrations(store)

    assert get_schema_version(store) == CURRENT_SCHEMA_VERSION
    assert _columns(store, "topic_pages") == TOPIC_PAGE_COLUMNS
    assert _columns(store, "findings") == FINDING_COLUMNS
    assert store.execute("SELECT id FROM emails").fetchall()[0][0] == email_id


def test_the_migration_runs_again_over_its_own_result(store):
    store.execute(
        "INSERT INTO findings (note_path, ordinal, claim, claim_key, reason, loaded_at)"
        " VALUES ('/n/a.md', 0, 'kept', 'kept', 'ok', '2026-10-16')"
    )
    store.commit()

    migrate_add_topic_pages(store)
    migrate_add_topic_pages(store)

    assert store.execute("SELECT claim FROM findings").fetchall()[0][0] == "kept"

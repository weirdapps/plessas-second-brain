"""v35: the state of extracted items, and room for a later consolidation pass.

Decisions had no status (0 of 133,722 email decisions had a date of their own), actions
could only be open or expired, and commitments held their text and two names. Schema v2
gives each item what it is (a decision made, proposed or requested; who owes an action;
the quote that states it), and nothing stored it. v35 adds the columns; v1 rows keep
NULL. canonical_id, superseded_by and invalid_at are for a consolidation pass that marks
items rather than deleting them, and key_facts.boilerplate for text that recurs across
threads (src/store/item_cleanup.py). Adding columns rewrites no row and rebuilds no
full-text index.
"""

import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    ITEM_STATE_COLUMNS,
    create_database,
    get_schema_version,
    migrate_add_item_state,
    run_migrations,
    set_schema_version,
)
from tests import factories as f

EXPECTED = {
    "decisions": [
        "status",
        "owner_role",
        "evidence",
        "canonical_id",
        "superseded_by",
        "invalid_at",
    ],
    "action_items": [
        "direction",
        "evidence",
        "closed_by",
        "closed_at",
        "canonical_id",
        "superseded_by",
        "invalid_at",
    ],
    "commitments": [
        "due",
        "direction",
        "status",
        "by_person_id",
        "to_person_id",
        "evidence",
        "canonical_id",
        "superseded_by",
        "invalid_at",
    ],
    "key_facts": ["canonical_id", "superseded_by", "invalid_at", "boilerplate"],
}


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def test_a_fresh_store_is_at_v35(store):
    assert CURRENT_SCHEMA_VERSION >= 35
    assert get_schema_version(store) == CURRENT_SCHEMA_VERSION


@pytest.mark.parametrize("table", sorted(EXPECTED))
def test_each_item_table_has_its_state_columns(store, table):
    columns = _columns(store, table)

    assert [c for c in columns if c in EXPECTED[table]] == EXPECTED[table]
    assert [name for name, _decl in ITEM_STATE_COLUMNS[table]] == EXPECTED[table]


def test_the_boilerplate_keys_have_a_table(store):
    assert _columns(store, "boilerplate_fact_keys") == ["key", "threads", "marked_at"]


def test_a_fact_is_not_boilerplate_until_marked(store):
    email_id = f.email(store, subject="Plan")
    store.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi costs 5')", (email_id,))

    assert store.execute("SELECT boilerplate FROM key_facts").fetchone()[0] == 0


def _drop_v35(conn):
    """The store as v34 left it: the v35 columns and table gone, the version stamped back."""
    conn.execute("DROP TABLE IF EXISTS boilerplate_fact_keys")
    for table, columns in ITEM_STATE_COLUMNS.items():
        for name, _decl in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    conn.commit()
    set_schema_version(conn, 34)


def test_migrating_a_v34_store_adds_the_columns_and_keeps_every_row(store):
    email_id = f.email(store, subject="Okapi plan")
    f.decision(store, "Adopt the okapi plan", email_id=email_id)
    f.action_item(store, "Draft the okapi budget", email_id=email_id)
    store.execute(
        "INSERT INTO commitments (email_id, commitment, by_person, to_person)"
        " VALUES (?, 'Send the okapi deck', 'Alice', 'Bob')",
        (email_id,),
    )
    store.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi costs 5')", (email_id,))
    store.commit()
    _drop_v35(store)
    before = {
        table: store.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in EXPECTED
    }

    run_migrations(store)

    assert get_schema_version(store) == CURRENT_SCHEMA_VERSION
    for table, rows in before.items():
        assert [c for c in _columns(store, table) if c in EXPECTED[table]] == EXPECTED[table]
        width = len(rows[0])
        after = store.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        assert [tuple(r)[:width] for r in after] == [tuple(r) for r in rows]
    # v1 rows keep NULL, and a fact is not boilerplate.
    assert tuple(store.execute("SELECT status, evidence FROM decisions").fetchone()) == (
        None,
        None,
    )
    assert store.execute("SELECT boilerplate FROM key_facts").fetchone()[0] == 0


def test_the_migration_rebuilds_no_full_text_index_and_rewrites_no_row(store):
    email_id = f.email(store, subject="Okapi plan")
    store.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi costs 5')", (email_id,))
    store.commit()
    _drop_v35(store)
    statements: list[str] = []
    store.set_trace_callback(statements.append)

    migrate_add_item_state(store)

    store.set_trace_callback(None)
    said = " ".join(statements).lower()
    assert "rebuild" not in said
    assert "update " not in said and "insert into key_facts" not in said
    assert "drop table" not in said and "create virtual" not in said
    store.execute("INSERT INTO key_facts_fts(key_facts_fts) VALUES('integrity-check')")
    found = store.execute("SELECT COUNT(*) FROM key_facts_fts WHERE key_facts_fts MATCH 'okapi'")
    assert found.fetchone()[0] == 1


def test_marking_a_fact_boilerplate_does_not_reindex_it(store):
    """key_facts_au fires on an UPDATE of id or fact only (v34)."""
    email_id = f.email(store, subject="Plan")
    store.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi costs 5')", (email_id,))
    store.commit()
    statements: list[str] = []
    store.set_trace_callback(statements.append)

    store.execute("UPDATE key_facts SET boilerplate = 1, canonical_id = 1")

    store.set_trace_callback(None)
    assert not any("key_facts_fts" in s for s in statements)


def _objects(conn):
    return sorted(map(tuple, conn.execute("SELECT type, name, sql FROM sqlite_master")))


def test_running_it_again_changes_nothing(store):
    schema = _objects(store)

    migrate_add_item_state(store)
    migrate_add_item_state(store)

    assert _objects(store) == schema


def test_a_store_without_an_item_table_is_left_alone(tmp_path):
    conn = sqlite3.connect(tmp_path / "bare.db")

    migrate_add_item_state(conn)

    assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [
        ("boilerplate_fact_keys",)
    ]

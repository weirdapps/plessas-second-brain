"""v38: consolidation state, entities and canonical topics.

The tables a consolidation pass, the entity seed and the topic families write
(src/store/consolidate.py, src/store/entities.py, src/store/topic_canon.py). Nothing
existing is rewritten: two columns are added to commitments, the rest is new tables
and partial indexes that start empty, and no full-text index is touched.
"""

import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    create_database,
    get_schema_version,
    migrate_add_memory,
    run_migrations,
    set_schema_version,
)
from tests import factories as f

NEW_TABLES = {
    "consolidation_threads": [
        "thread_key",
        "signature",
        "last_at",
        "rule_at",
        "model_signature",
        "model_at",
        "model_state",
        "attempts",
        "labels",
        "review",
    ],
    "item_labels": [
        "id",
        "item_table",
        "item_id",
        "label",
        "target_id",
        "target_ref",
        "evidence",
        "prior",
        "method",
        "prompt_hash",
        "reply_id",
        "thread_key",
        "run_id",
        "created_at",
        "undone_at",
    ],
    "entities": ["id", "kind", "name", "person_id", "method", "created_at"],
    "entity_aliases": ["kind", "value", "entity_id", "method", "score", "created_at"],
    "entity_adjudications": ["key", "verdict", "detail", "decided_at"],
    "topic_family": ["topic_id", "head_id", "method", "score", "mapped_at"],
}


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def test_a_fresh_store_is_at_the_current_version(store):
    assert CURRENT_SCHEMA_VERSION >= 38
    assert get_schema_version(store) == CURRENT_SCHEMA_VERSION


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_each_new_table_has_its_columns(store, table):
    assert _columns(store, table) == NEW_TABLES[table]


def test_commitments_can_record_what_closed_them(store):
    columns = _columns(store, "commitments")

    assert columns[-2:] == ["closed_by", "closed_at"]


def test_the_kinds_and_labels_are_checked(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO entities (kind, name, method, created_at) VALUES ('robot', 'x', 'm', 't')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO item_labels (item_table, item_id, label, method, thread_key, run_id,"
            " created_at) VALUES ('decisions', 1, 'deleted', 'rule', 'k', 'r', 't')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO topic_family (topic_id, head_id, method, mapped_at)"
            " VALUES (1, 2, 'guess', 't')"
        )


def _drop_v38(conn):
    for table in NEW_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for column in ("closed_by", "closed_at"):
        conn.execute(f"ALTER TABLE commitments DROP COLUMN {column}")
    for index in [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%_state_%'"
        )
    ]:
        conn.execute(f"DROP INDEX {index}")
    conn.commit()
    set_schema_version(conn, 35)


def test_migrating_an_older_store_keeps_every_row(store):
    email_id = f.email(store, subject="Okapi plan")
    f.decision(store, "Adopt the okapi plan", email_id=email_id)
    store.execute(
        "INSERT INTO commitments (email_id, commitment) VALUES (?, 'Send the okapi deck')",
        (email_id,),
    )
    store.commit()
    _drop_v38(store)
    before = store.execute("SELECT * FROM commitments").fetchall()

    run_migrations(store)

    assert get_schema_version(store) == CURRENT_SCHEMA_VERSION
    after = store.execute("SELECT * FROM commitments").fetchall()
    assert [tuple(r)[: len(before[0])] for r in after] == [tuple(r) for r in before]
    assert tuple(after[0])[-2:] == (None, None)
    for table, columns in NEW_TABLES.items():
        assert _columns(store, table) == columns


def test_the_migration_rebuilds_no_full_text_index_and_rewrites_no_row(store):
    email_id = f.email(store, subject="Okapi plan")
    store.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi costs 5')", (email_id,))
    store.commit()
    _drop_v38(store)
    statements: list[str] = []
    store.set_trace_callback(statements.append)

    migrate_add_memory(store)

    store.set_trace_callback(None)
    said = " ".join(statements).lower()
    assert "rebuild" not in said
    assert "update " not in said and "insert into" not in said
    assert "create virtual" not in said


def test_the_state_indexes_are_partial(store):
    indexes = dict(
        store.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%_state_%'"
        ).fetchall()
    )

    assert len(indexes) == 8  # canonical_id and superseded_by on four tables
    assert all("WHERE" in sql for sql in indexes.values())


def _objects(conn):
    return sorted(map(tuple, conn.execute("SELECT type, name, sql FROM sqlite_master")))


def test_running_it_again_changes_nothing(store):
    schema = _objects(store)

    migrate_add_memory(store)
    migrate_add_memory(store)

    assert _objects(store) == schema


def test_a_store_without_the_item_tables_gets_the_new_tables_only(tmp_path):
    conn = sqlite3.connect(tmp_path / "bare.db")

    migrate_add_memory(conn)

    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == set(NEW_TABLES)

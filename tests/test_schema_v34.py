"""v34: the full-text triggers re-index a row only when its indexed text changes, and the
people lookups the loader and the dedup make are served by an index.

Every `*_au` trigger fired on an UPDATE of any column and deleted and re-inserted the row's
whole text: a mailbox move re-tokenised a body, and each llm_status change of an attachment
(on every retry, deferral and success) up to 2M characters, twice. And `LOWER(email) = ?`,
asked for every sender, recipient and cc of every loaded email, read the whole people table.
"""

import re
import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.dedup_people import phase5_merge_email_as_name
from src.store.normalizer import find_or_create_person
from src.store.schema import (
    _FOLDED_FTS,
    create_database,
    get_schema_version,
    migrate_fts_update_triggers_on_source_columns,
    migrate_index_people_lookups,
    run_migrations,
    set_schema_version,
)

# A column of each table that its full-text index does not hold, set the way the pipelines set
# it: a status, a timestamp, a mailbox move, a payload.
UNINDEXED = {
    "emails": ("mailbox_name", "Archive"),
    "attachment_content": ("llm_status", "failed"),
    "key_facts": ("attachment_id", 7),
    "calendar_events": ("llm_status", "extracted"),
    "teams_messages": ("raw_json", "{}"),
    "teams_threads": ("extraction_status", "extracted"),
    "conversations": ("ended_at", "2026-10-02T00:00:00"),
    "conversation_turns": ("sentiment", "neutral"),
    "whatsapp_messages": ("media_type", "image"),
    "whatsapp_threads": ("embedding_at", "2026-10-02T00:00:00"),
}
TABLES = [(fts, table) for fts, table, _columns in _FOLDED_FTS]
PEOPLE_INDEXES = {"idx_people_email_lower", "idx_people_name_email"}


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _source_columns(conn, fts):
    """The content-table columns behind an index: each folded `<column>_f` is read from `<column>`."""
    return [r[1].removesuffix("_f") for r in conn.execute(f"PRAGMA table_info({fts})")]


def _insert(conn, table, words):
    """One row of `table` with `words` in its indexed columns, the other required columns filled."""
    values = dict(words)
    for _cid, name, decl, notnull, default, pk, hidden in conn.execute(
        f"PRAGMA table_xinfo({table})"
    ):
        if hidden or pk or name in values or not notnull or default is not None:
            continue
        values[name] = 1 if any(t in (decl or "").upper() for t in ("INT", "BOOL")) else "x"
    if table == "teams_threads":
        values["thread_kind"] = "chat_session"
    names = ", ".join(f'"{n}"' for n in values)
    marks = ", ".join("?" for _ in values)
    conn.execute("PRAGMA foreign_keys = OFF")
    row_id = conn.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values())
    ).lastrowid
    conn.commit()
    return row_id


def _seed(conn, table, fts):
    """A row whose indexed columns hold one word each: alpha0, alpha1, ..."""
    sources = _source_columns(conn, fts)
    return _insert(conn, table, {c: f"alpha{i}" for i, c in enumerate(sources)}), sources


def _index_pages(conn, fts):
    return conn.execute(f"SELECT id, block FROM {fts}_data ORDER BY id").fetchall()


def _matches(conn, fts, word):
    return [r[0] for r in conn.execute(f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?", (word,))]


def _consistent(conn, fts):
    """The index is internally sound and holds exactly what the content table holds.

    rank 1: the bare 'integrity-check' does not compare an external-content index with its
    table, so it passes on an index that a skipped trigger left stale.
    """
    conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")


def _schema(conn):
    return sorted(
        tuple(r) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
    )


def _as_v33(conn):
    """Put a store back the way v33 left it: every _au trigger fires on any column, and the
    people lookups have no index of their own."""
    for _fts, table in TABLES:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (f"{table}_au",)
        ).fetchone()[0]
        conn.execute(f"DROP TRIGGER {table}_au")
        conn.execute(re.sub(r"AFTER UPDATE OF .*? ON ", "AFTER UPDATE ON ", sql, count=1))
    for name in PEOPLE_INDEXES:
        conn.execute(f"DROP INDEX {name}")
    set_schema_version(conn, 33)


# --- the triggers ----------------------------------------------------------------------------


def test_every_full_text_index_of_the_store_is_covered():
    """The two tests below run over _FOLDED_FTS; a new index must join it to be covered."""
    conn = create_database(":memory:")
    found = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%USING fts5%'"
        )
    }
    conn.close()
    assert found == {fts for fts, _table in TABLES}
    assert set(UNINDEXED) == {table for _fts, table in TABLES}


@pytest.mark.parametrize(("fts", "table"), TABLES)
def test_an_update_of_a_column_the_index_does_not_hold_leaves_the_index_alone(store, fts, table):
    row_id, _sources = _seed(store, table, fts)
    before = _index_pages(store, fts)
    column, value = UNINDEXED[table]

    store.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))
    store.commit()

    assert _index_pages(store, fts) == before, f"{table}: the update re-wrote {fts}"
    assert _matches(store, fts, "alpha0") == [row_id]
    _consistent(store, fts)


@pytest.mark.parametrize(("fts", "table"), TABLES)
def test_an_update_of_each_indexed_column_re_indexes_the_row(store, fts, table):
    """Each one: SQLite does not check the column names of UPDATE OF, so a trigger that named a
    column wrongly would never fire for it, and nothing else would say so."""
    row_id, sources = _seed(store, table, fts)

    for i, column in enumerate(sources):
        store.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (f"omega{i}", row_id))
        store.commit()

        assert _matches(store, fts, f"omega{i}") == [row_id], column
        assert _matches(store, fts, f"alpha{i}") == [], column
        _consistent(store, fts)


def test_a_mixed_update_re_indexes_and_a_deleted_row_leaves_the_index(store):
    row_id, _sources = _seed(store, "emails", "emails_fts")

    store.execute(
        "UPDATE emails SET mailbox_name = 'Archive', subject = 'omega' WHERE id = ?", (row_id,)
    )
    store.commit()
    assert _matches(store, "emails_fts", "omega") == [row_id]
    store.execute("DELETE FROM emails WHERE id = ?", (row_id,))
    store.commit()

    assert _matches(store, "emails_fts", "omega") == []
    _consistent(store, "emails_fts")


def test_a_new_rowid_moves_the_row_in_the_index(store):
    """The index is keyed by the row id, so a change of id is a change the trigger must see."""
    row_id, _sources = _seed(store, "key_facts", "key_facts_fts")

    store.execute("UPDATE key_facts SET id = ? WHERE id = ?", (row_id + 100, row_id))
    store.commit()

    assert _matches(store, "key_facts_fts", "alpha0") == [row_id + 100]
    _consistent(store, "key_facts_fts")


# --- the people lookups ----------------------------------------------------------------------


def _plan(conn, sql):
    return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))


def _people_reads(conn, action):
    """The SELECTs on people that `action` makes, with their values bound in."""
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        action()
    finally:
        conn.set_trace_callback(None)
    return [s for s in seen if s.lstrip().upper().startswith("SELECT") and "people" in s]


def test_the_loader_finds_a_person_by_address_through_the_index(store):
    store.execute("INSERT INTO people (name, email) VALUES ('Example Person', 'a.person@x.test')")
    store.commit()

    reads = _people_reads(
        store, lambda: find_or_create_person(store, "Example Person", "A.Person@X.test")
    )

    assert reads
    for sql in reads:
        plan = _plan(store, sql)
        assert "SCAN people" not in plan, (sql, plan)
        assert "idx_people_email_lower" in plan, (sql, plan)


def test_the_loader_finds_a_person_by_name_through_the_index(store):
    """A name the model extracted, or an address not on record: both look the name up."""
    store.execute("INSERT INTO people (name) VALUES ('Example Person')")
    store.commit()

    reads = _people_reads(store, lambda: find_or_create_person(store, "Example Person"))
    reads += _people_reads(
        store, lambda: find_or_create_person(store, "Example Person", "new.person@x.test")
    )

    assert len(reads) >= 3
    for sql in reads:
        assert "SCAN people" not in _plan(store, sql), sql
    assert any("idx_people_name_email" in _plan(store, sql) for sql in reads)


def test_the_dedup_finds_an_address_through_the_index(store):
    store.execute("INSERT INTO people (name, email) VALUES ('Example Person', 'p@x.test')")
    store.execute("INSERT INTO people (name, email) VALUES ('p@x.test', NULL)")
    store.commit()

    reads = _people_reads(store, lambda: phase5_merge_email_as_name(store))

    lookups = [sql for sql in reads if "LOWER(email)" in sql]
    assert lookups
    for sql in lookups:
        assert "idx_people_email_lower" in _plan(store, sql), sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM people WHERE LOWER(email) = 'x'",
        "SELECT id FROM people WHERE LOWER(email) = LOWER('X')",
        "SELECT id FROM people WHERE LOWER(email) = LOWER('X') LIMIT 1",
    ],
)
def test_every_spelling_of_the_address_lookup_uses_the_index(store, sql):
    assert "idx_people_email_lower" in _plan(store, sql)


def test_the_lookup_still_matches_an_address_stored_in_capitals(store):
    """Same semantics as before: the comparison is LOWER(email), whatever case was stored."""
    store.execute("INSERT INTO people (name, email) VALUES ('Example Person', 'Mixed@X.test')")
    store.commit()
    (person_id,) = store.execute("SELECT id FROM people").fetchone()

    assert find_or_create_person(store, "Example Person", "mixed@x.test") == person_id
    assert store.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1


# --- the migration ---------------------------------------------------------------------------


def test_a_fresh_store_is_at_v34(store):
    assert CURRENT_SCHEMA_VERSION >= 34
    assert get_schema_version(store) == CURRENT_SCHEMA_VERSION
    assert PEOPLE_INDEXES <= {r[1] for r in store.execute("PRAGMA index_list(people)")}


def test_a_v33_store_migrates_to_what_a_fresh_store_is(tmp_path):
    fresh = create_database(str(tmp_path / "fresh.db"))
    old = create_database(str(tmp_path / "old.db"))
    _as_v33(old)
    assert _schema(old) != _schema(fresh)

    run_migrations(old)

    assert get_schema_version(old) == CURRENT_SCHEMA_VERSION
    assert _schema(old) == _schema(fresh)
    fresh.close()
    old.close()


def test_the_migration_is_idempotent(store):
    before = _schema(store)

    migrate_fts_update_triggers_on_source_columns(store)
    migrate_index_people_lookups(store)
    migrate_fts_update_triggers_on_source_columns(store)
    migrate_index_people_lookups(store)

    assert _schema(store) == before


def test_the_migration_rebuilds_no_index_and_keeps_every_index_in_step(store):
    """Triggers and two small indexes only: what the full-text indexes hold is untouched."""
    rows = {table: _seed(store, table, fts)[0] for fts, table in TABLES}
    _as_v33(store)
    pages = {fts: _index_pages(store, fts) for fts, _table in TABLES}

    run_migrations(store)

    for fts, table in TABLES:
        assert _index_pages(store, fts) == pages[fts], fts
        assert _matches(store, fts, "alpha0") == [rows[table]], fts
        _consistent(store, fts)


def test_the_migration_does_nothing_without_the_tables():
    conn = sqlite3.connect(":memory:")
    migrate_fts_update_triggers_on_source_columns(conn)
    migrate_index_people_lookups(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_an_index_over_a_column_its_table_lacks_is_refused_whole(store):
    """SQLite takes UPDATE OF over a column that does not exist and never fires for it. So an
    index whose source column cannot be found is refused, in one transaction: no table is left
    without its update trigger, and none gets one that would let its index drift."""
    _as_v33(store)
    store.execute("ALTER TABLE whatsapp_threads RENAME COLUMN title TO heading")
    store.commit()
    before = _schema(store)

    with pytest.raises(RuntimeError, match="whatsapp_threads"):
        migrate_fts_update_triggers_on_source_columns(store)

    assert _schema(store) == before


def test_a_failure_part_way_leaves_every_trigger_as_it_was(store):
    _as_v33(store)
    store.commit()
    before = _schema(store)
    last = f"{TABLES[-1][1]}_au"

    def refuse_the_last(action, name, *_rest):
        refused = action == sqlite3.SQLITE_CREATE_TRIGGER and name == last
        return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK

    store.set_authorizer(refuse_the_last)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            migrate_fts_update_triggers_on_source_columns(store)
    finally:
        store.set_authorizer(None)

    assert _schema(store) == before

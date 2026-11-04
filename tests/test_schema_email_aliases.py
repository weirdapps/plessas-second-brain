"""v30: the other Graph ids a stored email went by.

Moving a message to another folder gives it a new Graph id. The Archive export
stages the moved copy under that id, and the loader dropped it as a duplicate of
the email it already held (same RFC822 Message-ID) without noting the new id, so
the attachment directory downloaded under it could never be matched to an email.
"""

import sqlite3

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    create_database,
    get_connection,
    get_schema_version,
    migrate_add_email_aliases,
    run_migrations,
    set_schema_version,
)


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _email(conn, message_id="m1"):
    return conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, '2026-05-04', 's')",
        (message_id,),
    ).lastrowid


def test_a_fresh_store_has_the_alias_table(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    assert CURRENT_SCHEMA_VERSION >= 30
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert {"message_id", "email_id", "recorded_at"} <= _cols(conn, "email_aliases")
    conn.close()


def test_the_migration_is_idempotent(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    migrate_add_email_aliases(conn)
    migrate_add_email_aliases(conn)
    assert "email_id" in _cols(conn, "email_aliases")
    conn.close()


def test_it_upgrades_a_store_that_predates_it(tmp_path):
    path = str(tmp_path / "old.db")
    conn = create_database(path)
    conn.execute("DROP TABLE email_aliases")
    set_schema_version(conn, 29)
    conn.close()

    conn = get_connection(path)
    run_migrations(conn)

    assert "email_id" in _cols(conn, "email_aliases")
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    conn.close()


def test_an_alias_is_unique_and_goes_with_its_email(tmp_path):
    path = str(tmp_path / "b.db")
    create_database(path).close()
    conn = get_connection(path)  # foreign keys on, as every writer opens it
    email_id = _email(conn)
    conn.execute(
        "INSERT INTO email_aliases (message_id, email_id, recorded_at) VALUES ('copy', ?, 'now')",
        (email_id,),
    )
    try:
        conn.execute(
            "INSERT INTO email_aliases (message_id, email_id, recorded_at)"
            " VALUES ('copy', ?, 'now')",
            (email_id,),
        )
        raise AssertionError("an alias names one email")
    except sqlite3.IntegrityError:
        pass

    conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))

    assert conn.execute("SELECT count(*) FROM email_aliases").fetchone()[0] == 0
    conn.close()

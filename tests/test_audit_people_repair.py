"""scripts/repair_people.py mends the rows the old name and address rules broke.

Seven people rows kept garbled names for good under 'the longer name wins'
(schema-load-1, mcp-2), and senders linked to a namesake never had their address
saved (db-integrity-1). The code no longer does either; this one-off repairs what
it already did. Temp databases and synthetic people only.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from src.store.schema import create_database

GREEK = "ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ"  # the gauntlet's synthetic placeholder
GARBLED = GREEK.encode("gbk").decode("iso8859_10")


def _script():
    path = Path(__file__).parent.parent / "scripts" / "repair_people.py"
    spec = importlib.util.spec_from_file_location("repair_people", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _person(conn, name, email=None):
    return conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email)).lastrowid


def _sent(conn, person_id, address, sender_name="x"):
    n = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] + 1
    email_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, sender_name, sender_address) "
        "VALUES (?, '2026-09-01T10:00:00', ?, ?)",
        (n, sender_name, address),
    ).lastrowid
    if person_id is not None:
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'sender')",
            (email_id, person_id),
        )


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    ids = {
        "recoverable": _person(conn, GARBLED, "a@example.com"),
        "by_sender": _person(conn, GARBLED[:-1], "b@example.com"),
        "by_canonical": _person(conn, GARBLED[:-1], "c@example.com"),
        "backfill": _person(conn, "Delta Person"),
        "two_addresses": _person(conn, "Echo Person"),
        "held": _person(conn, "Foxtrot Person"),
        "holder": _person(conn, "Foxtrot Other", "f@example.com"),
        "shared_1": _person(conn, "Golf Person"),
        "shared_2": _person(conn, "GOLF PERSON"),
        "clean": _person(conn, "Hotel Person", "h@example.com"),
    }
    _sent(conn, None, "b@example.com", "Bravo Person")
    _sent(conn, None, "b@example.com", "Bravo Person")
    _sent(conn, None, "b@example.com", "B. Person")
    _sent(conn, ids["backfill"], "D@example.com")
    _sent(conn, ids["backfill"], "d@example.com")
    _sent(conn, ids["two_addresses"], "e1@example.com")
    _sent(conn, ids["two_addresses"], "e2@example.com")
    _sent(conn, ids["held"], "f@example.com")
    _sent(conn, ids["shared_1"], "g@example.com")
    _sent(conn, ids["shared_2"], "g@example.com")
    conn.commit()
    conn.close()
    canonical = tmp_path / "canonical_people.json"
    canonical.write_text(
        json.dumps([{"canonical_name": "Charlie Canonical", "email": "C@example.com"}])
    )
    return db, canonical, ids


def _people(db):
    conn = sqlite3.connect(db)
    try:
        return {
            pid: (name, email)
            for pid, name, email in conn.execute("SELECT id, name, email FROM people")
        }
    finally:
        conn.close()


def _run(db, canonical, *extra):
    return _script().main(["--db", str(db), "--canonical", str(canonical), *extra])


def test_the_dry_run_prints_the_plan_and_writes_nothing(store, capsys):
    db, canonical, ids = store
    before = _people(db)

    assert _run(db, canonical) == 0

    assert _people(db) == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert GREEK in out
    assert "Bravo Person" in out
    assert "d@example.com" in out


def test_apply_renames_garbled_names(store):
    db, canonical, ids = store

    assert _run(db, canonical, "--apply") == 0

    people = _people(db)
    assert people[ids["recoverable"]] == (GREEK, "a@example.com")
    assert people[ids["by_sender"]] == ("Bravo Person", "b@example.com")
    assert people[ids["by_canonical"]] == ("Charlie Canonical", "c@example.com")
    assert people[ids["clean"]] == ("Hotel Person", "h@example.com")


def test_apply_backfills_only_an_unambiguous_address(store):
    db, canonical, ids = store

    assert _run(db, canonical, "--apply") == 0

    people = _people(db)
    assert people[ids["backfill"]] == ("Delta Person", "d@example.com")
    # Sent from two addresses: which one is theirs is not known.
    assert people[ids["two_addresses"]][1] is None
    # Someone else holds it.
    assert people[ids["held"]][1] is None
    # Two people without an address both sent from it.
    assert people[ids["shared_1"]][1] is None
    assert people[ids["shared_2"]][1] is None


def test_a_second_apply_changes_nothing(store, capsys):
    db, canonical, ids = store
    _run(db, canonical, "--apply")
    after = _people(db)
    capsys.readouterr()

    assert _run(db, canonical, "--apply") == 0

    assert _people(db) == after
    assert "Renamed: 0" in capsys.readouterr().out


def test_apply_refuses_a_replica(store, monkeypatch):
    db, canonical, ids = store
    before = _people(db)
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert _run(db, canonical, "--apply") == 2

    assert _people(db) == before


def test_apply_waits_for_the_write_lock(store, monkeypatch):
    db, canonical, ids = store
    before = _people(db)
    module = _script()
    monkeypatch.setattr(module, "BUSY_TIMEOUT_MS", 50)
    writer = sqlite3.connect(db)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            module.main(["--db", str(db), "--canonical", str(canonical), "--apply"])
    finally:
        writer.rollback()
        writer.close()

    assert _people(db) == before


def test_a_dry_run_opens_a_database_whose_path_holds_uri_characters(tmp_path, capsys):
    """The read-only dry run pasted the path into a SQLite URI, where a '#' or a
    '?' in a directory name cuts the path short or rewrites the query, so the
    connection opened the wrong file or refused mode=ro. The path is escaped."""
    odd = tmp_path / "backups #2 ?mode=rwc"
    odd.mkdir()
    db = odd / "brain.db"
    create_database(str(db)).close()

    assert _script().main(["--db", str(db), "--canonical", str(tmp_path / "none.json")]) == 0
    assert "DRY RUN" in capsys.readouterr().out

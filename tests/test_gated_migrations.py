"""Gated migrations (issue 168): named, recorded in gated_migrations, off schema_version.

The item ledger and the commitment parents run only through `brain maint migrate
--apply-gated`, which needs a backup (or a dry run that writes nothing), so a pull of new
code changes no row. Being named rather than numbered, they neither block nor reorder a
numbered migration added later. Readers work the same on a store with them and without.
Every value is invented.
"""

import os
import sqlite3
import time

import pytest

from src import config, mcp_call
from src.config import CURRENT_SCHEMA_VERSION
from src.store import ledger
from src.store.schema import (
    GATED_MIGRATIONS,
    apply_gated,
    create_database,
    get_schema_version,
    pending_gated,
    run_migrations,
    set_schema_version,
)
from tests import factories as f
from tests.brain_cli import brain
from tests.mcp_listing import allow_an_event_loop

OWNER = ("Olga Owner", "olga@corp.example")
ALICE = ("Alice Smith", "alice@corp.example")


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])


def _store(path, gated):
    db = create_database(str(path), gated=gated)
    mail = f.email(db, subject="Okapi budget", sender=ALICE, to=(OWNER,))
    f.action_item(
        db, "Send the okapi budget", email_id=mail, owner=OWNER[0], direction="owner_owes"
    )
    f.action_item(
        db, "Send the okapi budget", email_id=mail, owner=OWNER[0], direction="owner_owes"
    )
    f.decision(db, "Adopt the okapi plan", email_id=mail)
    if gated:
        ledger.reconcile(db)
    db.commit()
    return db


def _open(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# --- off the numbered line -------------------------------------------------------------------


def test_the_numbered_migrations_never_apply_a_gated_one(tmp_path):
    db = _store(tmp_path / "old.db", gated=False)
    run_migrations(db)
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
    assert pending_gated(db) == list(GATED_MIGRATIONS)
    assert "item_ledger" not in _tables(db)
    db.close()


def test_a_later_numbered_migration_runs_with_the_gated_ones_waiting_and_they_apply_after(
    tmp_path,
):
    """A numbered migration added after them (the store stamped past the head, as the next
    release's run_migrations would leave it) does not wait for the gated ones, and they
    still apply, in order, on top of it."""
    db = _store(tmp_path / "old.db", gated=False)
    set_schema_version(db, CURRENT_SCHEMA_VERSION + 1)
    run_migrations(db)
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION + 1
    assert pending_gated(db) == list(GATED_MIGRATIONS)

    assert apply_gated(db) == list(GATED_MIGRATIONS)
    db.commit()

    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION + 1
    assert pending_gated(db) == []
    assert ledger.enabled(db)
    assert ledger.takes_thread_commitments(db, "teams_thread_id")
    db.close()


def test_applying_again_changes_nothing(tmp_path):
    db = _store(tmp_path / "brain.db", gated=True)
    before = ledger.counts(db)
    assert apply_gated(db) == []
    run_migrations(db, gated=True)
    assert ledger.counts(db) == before
    db.close()


def test_a_store_stamped_43_by_the_numbered_build_is_renumbered(tmp_path):
    db = _store(tmp_path / "brain.db", gated=True)
    db.execute("DROP TABLE gated_migrations")
    set_schema_version(db, 43)

    run_migrations(db)

    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
    # The two steps that build held are recorded; thread_identity (issue 179) and
    # person_identity (issue 170) and extraction_item_kind (issue 211) came after it.
    assert pending_gated(db) == ["thread_identity", "person_identity", "extraction_item_kind"]
    db.close()


# --- the command -----------------------------------------------------------------------------


@pytest.fixture
def old_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    path = tmp_path / "old.db"
    _store(path, gated=False).close()
    return path


def test_apply_gated_refuses_without_a_backup(old_path, monkeypatch, capsys):
    code, _, err = brain(
        monkeypatch, capsys, "--db", str(old_path), "maint", "migrate", "--apply-gated"
    )
    assert code == 2 and "--backup" in err
    db = _open(old_path)
    assert "item_ledger" not in _tables(db)
    db.close()


def test_the_dry_run_prints_the_diff_and_writes_nothing(old_path, monkeypatch, capsys):
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(old_path), "maint", "migrate", "--apply-gated", "--dry-run"
    )
    assert code == 0
    assert "Dry run, rolled back: item_ledger, commitment_parents" in out
    assert "action_items canonical_id set: 0 -> 1  (+1)" in out
    assert "item_labels method ledger: 0 -> 1  (+1)" in out
    db = _open(old_path)
    assert "item_ledger" not in _tables(db) and "gated_migrations" not in _tables(db)
    assert "item_hash" not in {r[1] for r in db.execute("PRAGMA table_info(action_items)")}
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
    db.close()


def test_apply_gated_writes_the_backup_first(old_path, tmp_path, monkeypatch, capsys):
    backup = tmp_path / "snap" / "before-gated.db"
    code, out, _ = brain(
        monkeypatch,
        capsys,
        "--db",
        str(old_path),
        "maint",
        "migrate",
        "--apply-gated",
        "--backup",
        str(backup),
    )
    assert code == 0 and "written now" in out and "Applied: item_ledger, commitment_parents" in out
    copy = _open(backup)
    assert "item_ledger" not in _tables(copy)
    assert copy.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 2
    copy.close()
    db = _open(old_path)
    assert pending_gated(db) == []
    assert db.execute("SELECT backup FROM gated_migrations LIMIT 1").fetchone()[0] == str(backup)
    db.close()


def test_a_backup_over_an_hour_old_is_refused(old_path, tmp_path, monkeypatch, capsys):
    backup = tmp_path / "stale.db"
    sqlite3.connect(old_path).backup(sqlite3.connect(backup))
    stale = time.time() - 7200
    os.utime(backup, (stale, stale))
    code, _, err = brain(
        monkeypatch,
        capsys,
        "--db",
        str(old_path),
        "maint",
        "migrate",
        "--apply-gated",
        "--backup",
        str(backup),
    )
    assert code == 2 and "h old" in err
    db = _open(old_path)
    assert "item_ledger" not in _tables(db)
    db.close()


def test_a_fresh_backup_is_taken(old_path, tmp_path, monkeypatch, capsys):
    backup = tmp_path / "fresh.db"
    with sqlite3.connect(old_path) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    code, out, _ = brain(
        monkeypatch,
        capsys,
        "--db",
        str(old_path),
        "maint",
        "migrate",
        "--apply-gated",
        "--backup",
        str(backup),
    )
    assert code == 0 and "min ago" in out


def test_the_plain_command_names_what_waits(old_path, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "--db", str(old_path), "maint", "migrate")
    assert code == 0 and "Gated, not applied: item_ledger, commitment_parents" in out


def test_a_replica_never_applies_them(old_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    code, _, _ = brain(
        monkeypatch, capsys, "--db", str(old_path), "maint", "migrate", "--apply-gated", "--dry-run"
    )
    assert code == 2


def test_dry_run_alone_is_refused(old_path, monkeypatch, capsys):
    code, _, err = brain(
        monkeypatch, capsys, "--db", str(old_path), "maint", "migrate", "--dry-run"
    )
    assert code == 2 and "--apply-gated" in err


# --- readers, on a store with the gated steps and without, as a replica reads it -----------

READS = [
    ("recall", {"query": "okapi"}),
    ("query_actions", {}),
    ("query_decisions", {}),
    ("search_emails", {"query": "okapi"}),
    ("stale_threads", {}),
    ("person_context", {"name_or_email": ALICE[1]}),
    ("topic_context", {"topic": "okapi"}),
    ("stats", {}),
]


@pytest.mark.parametrize("gated", [False, True], ids=["v41", "gated"])
def test_readers_answer_the_same_on_either_store(tmp_path, monkeypatch, gated):
    path = tmp_path / "brain.db"
    _store(path, gated=gated).close()
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    monkeypatch.setenv("BRAIN_EMBED_BACKEND", "none")
    allow_an_event_loop(monkeypatch)
    answers = {}
    with mcp_call._serving(path):
        for name, arguments in READS:
            answers[name] = mcp_call.call(name, arguments)
    actions = answers["query_actions"]
    tasks = [row["task"] for row in actions["result"]]
    # The ledger marks the second copy a duplicate, which the default read leaves out.
    assert tasks.count("Send the okapi budget") == (1 if gated else 2)
    assert "decisions" in str(answers["query_decisions"]) or answers["query_decisions"]
    assert "okapi" in str(answers["recall"]).lower()

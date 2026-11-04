"""The consolidation pass per entity (issue 168, part 2): the same item, of one kind and one
status, with one counterpart, taken from two threads within ENTITY_WINDOW_DAYS, is one item.
The later becomes duplicate_of the earlier (method 'entity'), never hiding an item from the
owner's own mail behind someone else's. Rule only, no model call. Every value is invented.
"""

import sqlite3

import pytest

from src import config
from src.store import consolidate, entity_pass, ledger
from src.store.schema import create_database
from tests import factories as f

OWNER = ("Olga Owner", "olga@corp.example")
ALICE = ("Alice Smith", "alice@corp.example")
BOB = ("Bob Jones", "bob@corp.example")


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])


@pytest.fixture
def conn(tmp_path, owner):
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


def _row(conn, table, item_id):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return dict(cur.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone())


def _owed_by_alice(conn, task, day, sender=ALICE, subject=None, **columns):
    """An action the owner owes Alice, from a mail of its own thread dated `day`."""
    mail = f.email(
        conn,
        subject=subject or f"Thread {task} {day}",
        sender=sender,
        to=(OWNER,) if sender != OWNER else (ALICE,),
        date_received=f"{day}T09:00:00Z",
    )
    return f.action_item(
        conn, task, email_id=mail, owner=OWNER[0], direction="owner_owes", **columns
    )


def _ready(conn):
    ledger.reconcile(conn)
    conn.commit()


def test_the_same_open_action_for_one_counterpart_in_two_threads_is_one(conn):
    first = _owed_by_alice(conn, "Send the budget", "2026-10-01")
    again = _owed_by_alice(conn, "send the budget.", "2026-10-20")
    _ready(conn)

    result = entity_pass.run(conn, since=None)

    assert result["labels"] == 1
    assert _row(conn, "action_items", again)["canonical_id"] == first
    (label,) = conn.execute(
        "SELECT label, method, target_id FROM item_labels WHERE item_id = ? AND method = 'entity'",
        (again,),
    ).fetchall()
    assert tuple(label) == ("duplicate_of", "entity", first)
    assert consolidate.hidden_ids(conn, "action_items", [first, again]) == {again}


def test_another_counterpart_is_another_item(conn):
    _owed_by_alice(conn, "Send the budget", "2026-10-01")
    other = _owed_by_alice(conn, "Send the budget", "2026-10-02", sender=BOB)
    _ready(conn)

    assert entity_pass.run(conn, since=None)["labels"] == 0
    assert _row(conn, "action_items", other)["canonical_id"] is None


def test_outside_the_window_it_is_asked_again(conn):
    _owed_by_alice(conn, "Send the monthly report", "2026-08-01")
    later = _owed_by_alice(conn, "Send the monthly report", "2026-10-01")
    _ready(conn)

    assert entity_pass.run(conn, since=None)["labels"] == 0
    assert _row(conn, "action_items", later)["canonical_id"] is None


def test_the_window_runs_from_the_first_not_from_the_last(conn):
    first = _owed_by_alice(conn, "Send the budget", "2026-08-01")
    second = _owed_by_alice(conn, "Send the budget", "2026-08-25")
    third = _owed_by_alice(conn, "Send the budget", "2026-09-15")
    _ready(conn)

    entity_pass.run(conn, since=None)

    assert _row(conn, "action_items", second)["canonical_id"] == first
    assert _row(conn, "action_items", third)["canonical_id"] is None


def test_a_done_action_does_not_absorb_an_open_one(conn):
    done = _owed_by_alice(conn, "Send the budget", "2026-10-01")
    conn.execute(
        "UPDATE action_items SET status = 'done', closed_by = 'owner' WHERE id = ?", (done,)
    )
    still = _owed_by_alice(conn, "Send the budget", "2026-10-05")
    _ready(conn)

    assert entity_pass.run(conn, since=None)["labels"] == 0
    assert _row(conn, "action_items", still)["canonical_id"] is None


def test_an_item_from_the_owners_mail_is_never_hidden_behind_another(conn):
    theirs = _owed_by_alice(conn, "Send the budget", "2026-10-01")
    mine = _owed_by_alice(conn, "Send the budget", "2026-10-03", sender=OWNER)
    _ready(conn)
    # The owner's own mail names Alice by a hard identifier only in a case this test
    # stands for: give both rows the same address-found counterpart.
    alice = _row(conn, "action_items", theirs)["counterpart_person_id"]
    conn.execute(
        "UPDATE action_items SET counterpart_person_id = ?, counterpart_via = 'address'"
        " WHERE id = ?",
        (alice, mine),
    )
    conn.commit()

    entity_pass.run(conn, since=None)

    assert _row(conn, "action_items", mine)["canonical_id"] is None


def test_a_counterpart_matched_by_name_alone_never_joins_two_threads(conn):
    """Issue 170: merge on a hard identifier only. Two same-hash actions owed to the owner
    by 'Alice Smith', a name the model wrote, stay two items, listed as a candidate."""
    f.person(conn, *ALICE)
    one = f.email(
        conn, subject="One", sender=OWNER, to=(ALICE,), date_received="2026-10-01T09:00:00Z"
    )
    two = f.email(
        conn, subject="Two", sender=OWNER, to=(ALICE,), date_received="2026-10-02T09:00:00Z"
    )
    first = f.action_item(
        conn, "Send the deck", email_id=one, owner=ALICE[0], direction="owed_to_owner"
    )
    again = f.action_item(
        conn, "Send the deck", email_id=two, owner=ALICE[0], direction="owed_to_owner"
    )
    _ready(conn)
    assert _row(conn, "action_items", first)["counterpart_via"] == "name"
    assert _row(conn, "action_items", again)["counterpart_person_id"] is not None

    result = entity_pass.run(conn, since=None)

    assert result["labels"] == 0
    assert result["name_only_candidates"] == 1
    assert _row(conn, "action_items", again)["canonical_id"] is None


def test_two_people_rows_of_one_entity_are_one_counterpart(conn):
    first = _owed_by_alice(conn, "Send the budget", "2026-10-01")
    other_alice = ("A. Smith", "a.smith@home.example")
    again = _owed_by_alice(conn, "Send the budget", "2026-10-04", sender=other_alice)
    _ready(conn)
    one, two = f.person(conn, *ALICE), f.person(conn, *other_alice)
    entity = conn.execute(
        "INSERT INTO entities (kind, name, method, created_at, person_id)"
        " VALUES ('person', 'Alice Smith', 'test', 'x', ?)",
        (one,),
    ).lastrowid
    for person in (one, two):
        conn.execute(
            "INSERT INTO entity_aliases (kind, value, entity_id, method, created_at)"
            " VALUES ('person', ?, ?, 'test', 'x')",
            (str(person), entity),
        )
    conn.commit()

    entity_pass.run(conn, since=None)

    assert _row(conn, "action_items", again)["canonical_id"] == first


def test_decisions_name_their_counterpart_by_name_only_and_stay_apart(conn):
    one = f.decision(conn, "Launch in April", decided_by=ALICE[0])
    two = f.decision(conn, "Launch in April", decided_by=ALICE[0])
    _ready(conn)
    assert _row(conn, "decisions", one)["counterpart_via"] == "name"

    entity_pass.run(conn, since=None)

    assert _row(conn, "decisions", two)["canonical_id"] is None


def test_the_hourly_pass_starts_where_it_first_ran(conn):
    _owed_by_alice(conn, "Send the budget", "2026-10-01")
    old_again = _owed_by_alice(conn, "Send the budget", "2026-10-02")
    _ready(conn)

    first = entity_pass.run_hourly(conn)
    assert first == {"enabled": True, "started_from_scratch": True, "labels": 0}
    assert _row(conn, "action_items", old_again)["canonical_id"] is None

    new = _owed_by_alice(conn, "Send the budget", "2026-10-03")
    _ready(conn)
    assert entity_pass.run_hourly(conn)["labels"] == 1
    assert _row(conn, "action_items", new)["canonical_id"] is not None


def test_a_run_reverts_by_its_id(conn):
    _owed_by_alice(conn, "Send the budget", "2026-10-01")
    again = _owed_by_alice(conn, "Send the budget", "2026-10-02")
    _ready(conn)
    result = entity_pass.run(conn, since=None)

    consolidate.revert(conn, result["run_id"])

    assert _row(conn, "action_items", again)["canonical_id"] is None


def test_nothing_runs_on_a_store_without_the_ledger(tmp_path, owner):
    db = create_database(str(tmp_path / "old.db"), gated=False)
    _owed_by_alice(db, "Send the budget", "2026-10-01")
    _owed_by_alice(db, "Send the budget", "2026-10-02")
    assert entity_pass.run(db, since=None) == {"enabled": False}
    assert entity_pass.run_hourly(db) == {"enabled": False}
    db.close()


def test_the_backfill_is_a_dry_run_unless_applied(tmp_path, owner, monkeypatch, capsys):
    from tests.brain_cli import brain

    path = tmp_path / "brain.db"
    db = create_database(str(path))
    _owed_by_alice(db, "Send the budget", "2026-10-01")
    again = _owed_by_alice(db, "Send the budget", "2026-10-02")
    _ready(db)
    db.close()
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "consolidate-entities")
    assert code == 0 and "dry run" in out and "1 label" in out
    check = sqlite3.connect(path)
    assert (
        check.execute("SELECT canonical_id FROM action_items WHERE id = ?", (again,)).fetchone()[0]
        is None
    )
    check.close()

    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(path), "maint", "consolidate-entities", "--apply"
    )
    assert code == 0 and "1 label" in out
    check = sqlite3.connect(path)
    assert check.execute("SELECT canonical_id FROM action_items WHERE id = ?", (again,)).fetchone()[
        0
    ]
    check.close()

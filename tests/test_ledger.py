"""The item ledger (schema v42, issue 168): when an item was seen, who it concerns, its
resolved due date, a hash of its normalised text, and a lifecycle status kept in a table
keyed by that hash, so extracting a parent again cannot reopen what was closed.

v42 is gated: the migrations every job runs on opening the store stop at v41, and only
`brain maint migrate --ledger` (or a new, empty store) takes v42. Every value is invented.
"""

import sqlite3
from datetime import date

import pytest

from src import config
from src.store import consolidate, ledger
from src.store.loader import replace_items, stored_email
from src.store.schema import (
    AUTO_SCHEMA_VERSION,
    create_database,
    get_schema_version,
    run_migrations,
)
from tests import factories as f

OWNER = ("Olga Owner", "olga@corp.example")
ALICE = ("Alice Smith", "alice@corp.example")


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])


@pytest.fixture
def conn(tmp_path, owner):
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


@pytest.fixture
def old(tmp_path, owner):
    """A store at v41: what every producer holds until the owner runs the gated step."""
    db = create_database(str(tmp_path / "old.db"), gated=False)
    yield db
    db.close()


def _row(conn, table, item_id):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return dict(cur.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone())


def _ledger(conn, table, item_id):
    row = _row(conn, table, item_id)
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    found = cur.execute(
        "SELECT * FROM item_ledger WHERE item_table = ? AND item_hash = ? AND parent_key = ?",
        (table, row["item_hash"], ledger.parent_key_of(row)),
    ).fetchone()
    return dict(found) if found else None


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# --- the hash ----------------------------------------------------------------------------


def test_normalised_text_ignores_case_accents_punctuation_and_spacing():
    assert ledger.item_hash("Στείλε τον  Προϋπολογισμό!") == ledger.item_hash(
        "στειλε τον προυπολογισμο"
    )
    assert ledger.item_hash("Send the budget.") == ledger.item_hash("  send THE budget ")


def test_different_text_hashes_differently():
    assert ledger.item_hash("Send the budget") != ledger.item_hash("Send the plan")
    assert ledger.item_hash("Pay 4,000") != ledger.item_hash("Pay 5,000")
    assert ledger.item_hash("") is None and ledger.item_hash(None) is None


# --- the due date --------------------------------------------------------------------------

SEEN = date(2026, 11, 4)  # a Wednesday


@pytest.mark.parametrize(
    ("text", "due"),
    [
        ("2026-11-20", "2026-11-20"),
        ("2026-11-20T17:00:00Z", "2026-11-20"),
        ("20/11/2026", "2026-11-20"),
        ("20.11.26", "2026-11-20"),
        ("by 20/11", "2026-11-20"),
        ("έως 15/1", "2027-01-15"),
        ("tomorrow", "2026-11-05"),
        ("αύριο", "2026-11-05"),
        ("today", "2026-11-04"),
        ("Friday", "2026-11-06"),
        ("μέχρι την Παρασκευή", "2026-11-06"),
        ("Monday", "2026-11-09"),
        ("end of month", "2026-11-30"),
        ("τέλος του μήνα", "2026-11-30"),
        ("end of week", "2026-11-06"),
        ("end of week (by 2026-11-20)", "2026-11-20"),
        ("Friday 10.30", "2026-11-06"),
        ("every Monday", None),
        ("2-3 days after the launch", None),
        ("within the next 3-4 days", None),
        ("20-11-2026", "2026-11-20"),
        ("early next week (from 2026-11-02)", None),
        ("within days (from 25/06/2021)", None),
        ("κάθε Δευτέρα", None),
        ("ASAP", None),
        ("Q4", None),
        ("31/02/2026", None),
        (None, None),
    ],
)
def test_resolve_due(text, due):
    assert ledger.resolve_due(text, SEEN) == due


def test_resolve_due_without_a_seen_date_takes_full_dates_only():
    assert ledger.resolve_due("20/11/2026", None) == "2026-11-20"
    assert ledger.resolve_due("Friday", None) is None


# --- the gate ------------------------------------------------------------------------------


def test_the_migrations_jobs_run_stop_before_the_ledger(old):
    assert get_schema_version(old) == AUTO_SCHEMA_VERSION == 41
    run_migrations(old)
    assert get_schema_version(old) == 41
    assert not ledger.enabled(old)
    assert "item_hash" not in _columns(old, "action_items")


def test_the_gated_steps_take_the_store_to_v43(old):
    run_migrations(old, gated=True)
    assert get_schema_version(old) == config.CURRENT_SCHEMA_VERSION == 43
    assert ledger.enabled(old)
    for table in ("decisions", "action_items", "commitments"):
        assert {"observed_at", "item_hash", "counterpart_person_id"} <= _columns(old, table)
    for table in ("action_items", "commitments"):
        assert {"due_date", "owner_role"} <= _columns(old, table)


def test_jobs_never_step_a_ledger_store_back(conn):
    assert get_schema_version(conn) == 43
    run_migrations(conn)
    assert get_schema_version(conn) == 43


def test_a_new_store_has_the_ledger(conn):
    assert ledger.enabled(conn)


def test_reconcile_does_nothing_on_a_store_without_the_ledger(old):
    f.action_item(old, "Send the budget")
    assert ledger.reconcile(old) == {"enabled": False}


# --- the backfill ------------------------------------------------------------------------


def test_the_gated_step_fills_existing_rows_and_their_status(old):
    mail = f.email(old, sender=ALICE, to=(OWNER,), date_received="2026-11-04T08:00:00Z")
    done = f.action_item(
        old, "Send the budget", email_id=mail, deadline="Friday", direction="owner_owes"
    )
    old.execute(
        "UPDATE action_items SET status = 'done', closed_by = 'owner', closed_at = ? WHERE id = ?",
        ("2026-11-05T10:00:00Z", done),
    )
    expired = f.action_item(old, "Book the room", email_id=mail, deadline="2026-01-01")
    old.execute("UPDATE action_items SET status = 'expired' WHERE id = ?", (expired,))
    first = f.decision(old, "Launch in April", email_id=mail)
    later = f.decision(old, "Launch in May", email_id=mail)
    old.execute(
        "UPDATE decisions SET superseded_by = ?, invalid_at = ? WHERE id = ?",
        (later, "2026-11-04T08:00:00Z", first),
    )
    alice = f.person(old, *ALICE)
    me = f.person(old, *OWNER)
    promise = old.execute(
        "INSERT INTO commitments (email_id, commitment, by_person_id, to_person_id, due,"
        " direction, status) VALUES (?, 'Share the deck', ?, ?, '10/11', 'owner_owes', 'open')",
        (mail, me, alice),
    ).lastrowid
    old.commit()

    run_migrations(old, gated=True)

    row = _row(old, "action_items", done)
    assert row["observed_at"] == "2026-11-04T08:00:00Z"
    assert row["item_hash"] == ledger.item_hash("Send the budget")
    assert row["due_date"] == "2026-11-06"
    assert row["owner_role"] == "assigned_to_owner"
    assert _ledger(old, "action_items", done)["status"] == "done"
    assert _ledger(old, "action_items", done)["status_by"] == "owner"
    assert _ledger(old, "action_items", expired)["status"] == "expired"
    assert _ledger(old, "decisions", first)["status"] == "superseded"
    assert _ledger(old, "decisions", later)["status"] == "open"
    commitment = _row(old, "commitments", promise)
    assert commitment["due_date"] == "2026-11-10"
    assert commitment["counterpart_person_id"] == alice
    assert commitment["owner_role"] == "assigned_to_owner"


# --- re-extraction keeps the status --------------------------------------------------------


def _extraction(*tasks, decisions=()):
    return {
        "summary": "s",
        "decisions": [{"decision": d} for d in decisions],
        "action_items": [{"task": t, "owner": ALICE[0]} for t in tasks],
        "commitments": [],
        "key_facts": [],
    }


def _reextract(conn, email_id, extraction):
    replace_items(conn, email_id, stored_email(conn, email_id), extraction)
    conn.commit()
    return ledger.reconcile(conn)


def _ids(conn, email_id, text):
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM action_items WHERE email_id = ? AND task = ? ORDER BY id",
            (email_id, text),
        )
    ]


def test_extracting_again_keeps_a_done_and_an_expired_action_closed(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    done = f.action_item(conn, "Send the budget", email_id=mail)
    expired = f.action_item(conn, "Book the room", email_id=mail)
    ledger.reconcile(conn)
    conn.execute(
        "UPDATE action_items SET status = 'done', closed_by = 'email:9', closed_at = ?"
        " WHERE id = ?",
        ("2026-11-05T10:00:00Z", done),
    )
    conn.execute("UPDATE action_items SET status = 'expired' WHERE id = ?", (expired,))
    conn.commit()
    ledger.reconcile(conn)

    _reextract(conn, mail, _extraction("send the  budget!", "Book the room", "Call Bob"))

    (new_done,) = _ids(conn, mail, "send the  budget!")
    row = _row(conn, "action_items", new_done)
    assert (row["status"], row["closed_by"], row["closed_at"]) == (
        "done",
        "email:9",
        "2026-11-05T10:00:00Z",
    )
    assert _row(conn, "action_items", _ids(conn, mail, "Book the room")[0])["status"] == "expired"
    assert _row(conn, "action_items", _ids(conn, mail, "Call Bob")[0])["status"] == "open"


def test_extracting_again_keeps_a_superseded_decision_hidden(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    first = f.decision(conn, "Launch in April", email_id=mail)
    later = f.decision(conn, "Launch in May", email_id=mail)
    conn.execute(
        "UPDATE decisions SET superseded_by = ?, invalid_at = ? WHERE id = ?",
        (later, "2026-11-04T08:00:00Z", first),
    )
    conn.commit()
    ledger.reconcile(conn)

    _reextract(conn, mail, _extraction(decisions=("Launch in April", "Launch in May")))

    rows = conn.execute(
        f"SELECT d.decision FROM decisions d WHERE d.email_id = ?"
        f" AND {consolidate.current_sql(conn, 'd', 'decisions')}",
        (mail,),
    ).fetchall()
    assert [r[0] for r in rows] == ["Launch in May"]


def test_an_item_extraction_no_longer_gives_is_dropped_and_comes_back_open(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    gone = f.action_item(conn, "Call Bob", email_id=mail)
    ledger.reconcile(conn)
    entry = _ledger(conn, "action_items", gone)

    _reextract(conn, mail, _extraction("Send the budget"))
    status = conn.execute(
        "SELECT status FROM item_ledger WHERE item_table = 'action_items'"
        " AND parent_key = ? AND item_hash = ?",
        (entry["parent_key"], entry["item_hash"]),
    ).fetchone()[0]
    assert status == "dropped"

    _reextract(conn, mail, _extraction("Send the budget", "Call Bob"))
    (back,) = _ids(conn, mail, "Call Bob")
    assert _row(conn, "action_items", back)["status"] == "open"
    assert _ledger(conn, "action_items", back)["status"] == "open"


def test_a_closed_action_is_never_reopened_by_its_row(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    action = f.action_item(conn, "Send the budget", email_id=mail)
    ledger.reconcile(conn)
    conn.execute(
        "UPDATE action_items SET status = 'done', closed_by = 'owner' WHERE id = ?", (action,)
    )
    conn.commit()
    ledger.reconcile(conn)
    # The daily age-out marks expired only what is open, but a hand edit could do anything.
    conn.execute("UPDATE action_items SET status = 'expired' WHERE id = ?", (action,))
    conn.commit()
    ledger.reconcile(conn)
    assert _ledger(conn, "action_items", action)["status"] == "done"


# --- the uniqueness rule ---------------------------------------------------------------


def test_an_exact_duplicate_in_one_parent_points_at_the_first(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    first = f.action_item(conn, "Send the budget", email_id=mail)
    again = f.action_item(conn, "send the budget.", email_id=mail)
    other = f.action_item(conn, "Send the budget")  # another email

    result = ledger.reconcile(conn)

    assert result["duplicates"] == 1
    assert _row(conn, "action_items", again)["canonical_id"] == first
    assert _row(conn, "action_items", first)["canonical_id"] is None
    assert _row(conn, "action_items", other)["canonical_id"] is None
    (label,) = conn.execute(
        "SELECT label, method, target_id FROM item_labels WHERE item_id = ?", (again,)
    ).fetchall()
    assert tuple(label) == ("duplicate_of", "ledger", first)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM item_ledger WHERE item_table = 'action_items'"
        ).fetchone()[0]
        == 2
    )


def test_the_ledger_holds_one_entry_per_parent_and_hash(conn):
    with pytest.raises(sqlite3.IntegrityError):
        for _ in range(2):
            conn.execute(
                "INSERT INTO item_ledger (item_table, parent_key, item_hash, status, status_at,"
                " first_seen_at) VALUES ('action_items', 'email:1', 'h', 'open', 'x', 'x')"
            )


def test_the_status_must_be_one_of_five(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO item_ledger (item_table, parent_key, item_hash, status, status_at,"
            " first_seen_at) VALUES ('action_items', 'email:1', 'h', 'closed', 'x', 'x')"
        )


# --- revert, forget --------------------------------------------------------------------


def test_a_reverted_label_reopens_the_ledger(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    first = f.decision(conn, "Launch in April", email_id=mail)
    later = f.decision(conn, "Launch in May", email_id=mail)
    ledger.reconcile(conn)
    label = consolidate.Label(
        "decisions", first, "superseded_by", "model", target_id=later, target_date="2026-11-04"
    )
    consolidate.write_label(conn, label, "email:x", "run-1")
    ledger.reconcile(conn)
    assert _ledger(conn, "decisions", first)["status"] == "superseded"

    consolidate.revert(conn, "run-1")

    assert _ledger(conn, "decisions", first)["status"] == "open"


def test_an_entry_whose_parent_is_gone_goes_too(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    f.action_item(conn, "Send the budget", email_id=mail)
    ledger.reconcile(conn)
    conn.execute("DELETE FROM action_items WHERE email_id = ?", (mail,))
    conn.execute("DELETE FROM email_people WHERE email_id = ?", (mail,))
    conn.execute("DELETE FROM emails WHERE id = ?", (mail,))
    conn.commit()

    assert ledger.reconcile(conn)["forgotten"] == 1
    assert conn.execute("SELECT COUNT(*) FROM item_ledger").fetchone()[0] == 0


# --- who and when ------------------------------------------------------------------------


def test_observed_at_is_the_parent_date_for_every_source(conn):
    mail = f.email(conn, date_received="2026-10-01T09:00:00Z")
    event = f.calendar_event(conn, start_at="2026-10-02T09:00:00Z")
    thread = f.teams_thread(conn, started_at="2026-10-03T09:00:00Z")
    a = f.action_item(conn, "A", email_id=mail)
    b = f.action_item(conn, "B", email_id=None, event_id=event)
    c = f.decision(conn, "C", email_id=None, teams_thread_id=thread)
    ledger.reconcile(conn)
    assert _row(conn, "action_items", a)["observed_at"] == "2026-10-01T09:00:00Z"
    assert _row(conn, "action_items", b)["observed_at"] == "2026-10-02T09:00:00Z"
    assert _row(conn, "decisions", c)["observed_at"] == "2026-10-03T09:00:00Z"


def test_the_counterpart_of_an_action_the_owner_owes_is_who_asked(conn):
    mail = f.email(conn, sender=ALICE, to=(OWNER,))
    action = f.action_item(
        conn, "Send the budget", email_id=mail, owner=OWNER[0], direction="owner_owes"
    )
    ledger.reconcile(conn)
    row = _row(conn, "action_items", action)
    assert row["counterpart_person_id"] == f.person(conn, *ALICE)
    assert row["owner_role"] == "assigned_to_owner"


def test_the_counterpart_of_an_action_owed_to_the_owner_is_its_owner(conn):
    alice = f.person(conn, *ALICE)
    mail = f.email(conn, sender=OWNER, to=(ALICE,))
    action = f.action_item(
        conn, "Send the deck", email_id=mail, owner=ALICE[0], direction="owed_to_owner"
    )
    ledger.reconcile(conn)
    row = _row(conn, "action_items", action)
    assert row["counterpart_person_id"] == alice
    assert row["owner_role"] == "owner_assigned_other"


# --- how the owner runs it, and where it runs after ----------------------------------------


def test_maint_migrate_alone_stops_at_v41_and_with_ledger_takes_the_gated_steps(
    tmp_path, owner, monkeypatch, capsys
):
    from tests.brain_cli import brain

    path = tmp_path / "old.db"
    create_database(str(path), gated=False).close()
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "migrate")
    assert code == 0 and "already up to date" in out
    check = sqlite3.connect(path)
    check.row_factory = sqlite3.Row
    assert get_schema_version(check) == 41
    check.close()

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "migrate", "--ledger")
    assert code == 0 and "41 to 43" in out
    check = sqlite3.connect(path)
    check.row_factory = sqlite3.Row
    assert get_schema_version(check) == 43
    check.close()


def test_the_daily_lifecycle_records_what_it_expired(tmp_path, owner):
    from src.store.action_lifecycle import run_action_lifecycle

    path = str(tmp_path / "brain.db")
    db = create_database(path)
    mail = f.email(db, date_received="2025-01-01T09:00:00Z")
    action = f.action_item(db, "Book the room", email_id=mail, deadline="2025-01-10")
    ledger.reconcile(db)
    db.commit()
    db.close()

    run_action_lifecycle(path)

    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    assert _ledger(db, "action_items", action)["status"] == "expired"
    db.close()


def test_the_hourly_consolidation_reconciles_first(tmp_path, owner, monkeypatch):
    from src.store.consolidate import run_in_sync

    monkeypatch.setenv("BRAIN_CONSOLIDATE_SYNC_TOKENS", "0")
    path = str(tmp_path / "brain.db")
    db = create_database(path)
    action = f.action_item(db, "Send the budget")
    db.close()

    run_in_sync(path, 30)

    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    assert _row(db, "action_items", action)["item_hash"] == ledger.item_hash("Send the budget")
    assert _ledger(db, "action_items", action)["status"] == "open"
    db.close()

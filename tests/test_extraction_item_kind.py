"""The gated migration `extraction_item_kind` (issue 211): emails.item_kind, what a schema v2
extraction says the email is (a work thread, a notification, bulk mail...), which until now
stayed only in the extraction file under data/extracted/.

Gated, not numbered: scripts/deploy.sh applies numbered migrations by itself, and a column
the owner has not taken must cost nothing. Without it the loader writes as before; with it,
a v2 extraction's item_kind is stored, a v1 one's is NULL, and so is a value outside the
vocabulary. Every value is invented.
"""

import pytest

import src.config as config
from src.store import extraction_fields
from src.store.loader import load_single_email, replace_extraction
from src.store.schema import (
    GATED_MIGRATIONS,
    apply_gated,
    create_database,
    pending_gated,
    run_migrations,
)
from tests.test_loader_item_state import V1_EXTRACTION, metadata, v2_reply


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", "owner@example.com")


def _columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(emails)")}


def _kind(conn, message_id="m-1"):
    row = conn.execute("SELECT item_kind FROM emails WHERE message_id = ?", (message_id,))
    return row.fetchone()[0]


@pytest.fixture
def bare(tmp_path):
    conn = create_database(str(tmp_path / "bare.db"), gated=False)
    yield conn
    conn.close()


@pytest.fixture
def gated(tmp_path):
    conn = create_database(str(tmp_path / "gated.db"))
    yield conn
    conn.close()


def test_it_is_gated_and_last():
    assert extraction_fields.NAME in GATED_MIGRATIONS
    assert GATED_MIGRATIONS[-1] == extraction_fields.NAME


def test_the_numbered_migrations_leave_it_out(bare):
    run_migrations(bare)

    assert "item_kind" not in _columns(bare)
    assert extraction_fields.NAME in pending_gated(bare)
    assert not extraction_fields.applied(bare)


def test_a_new_store_takes_it(gated):
    assert "item_kind" in _columns(gated)
    assert extraction_fields.applied(gated)


def test_apply_adds_the_column_and_a_rollback_leaves_none(bare):
    """As `brain maint migrate --apply-gated --dry-run` runs it: in a transaction."""
    bare.commit()
    bare.execute("BEGIN IMMEDIATE")
    assert extraction_fields.NAME in apply_gated(bare)
    assert "item_kind" in _columns(bare)

    bare.rollback()

    assert "item_kind" not in _columns(bare)


def test_the_step_is_idempotent(gated):
    extraction_fields.migrate(gated)

    assert "item_kind" in _columns(gated)


def test_without_the_step_a_v2_extraction_loads_as_before(bare):
    assert load_single_email(bare, metadata(), v2_reply())

    assert "item_kind" not in _columns(bare)


def test_with_the_step_a_v2_extraction_keeps_its_item_kind(gated):
    load_single_email(gated, metadata(), v2_reply(item_kind="notification"))

    assert _kind(gated) == "notification"


def test_a_v1_extraction_leaves_it_null(gated):
    load_single_email(gated, metadata(), {**V1_EXTRACTION, "item_kind": "notification"})

    assert _kind(gated) is None


def test_a_value_outside_the_vocabulary_is_null():
    """The parser defaults an unknown kind; a hand-made extraction file may still hold one."""
    extraction = {**v2_reply(), "item_kind": "spam"}

    assert extraction_fields.item_kind(extraction) is None
    assert extraction_fields.item_kind(v2_reply(item_kind="bulk")) == "bulk"


def test_a_replaced_extraction_replaces_the_kind(gated):
    load_single_email(gated, metadata(), v2_reply(item_kind="notification"))
    email_id = gated.execute("SELECT id FROM emails").fetchone()[0]

    replace_extraction(
        gated,
        email_id,
        metadata(),
        wrong=v2_reply(item_kind="notification"),
        right=v2_reply(item_kind="work_thread"),
    )

    assert _kind(gated) == "work_thread"


def test_a_replaced_extraction_on_a_bare_store_writes_no_kind(bare):
    load_single_email(bare, metadata(), v2_reply())
    email_id = bare.execute("SELECT id FROM emails").fetchone()[0]

    replace_extraction(bare, email_id, metadata(), wrong=v2_reply(), right=v2_reply())

    assert "item_kind" not in _columns(bare)


# --- the Latin name, in #170's identifier table ---------------------------------------


def _names(conn):
    return {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT value, source FROM person_identifiers WHERE kind = 'name'"
            " AND source = 'extraction_v2'"
        )
    }


def test_a_v2_person_keeps_the_name_as_written_and_the_latin_one(gated):
    """person_identifiers kind 'name' (issue 170): the Latin form a v2 extraction gives
    finds the person later under either spelling."""
    reply = v2_reply()
    reply["people"][1] = {
        **reply["people"][1],
        "name_as_written": "Άννα Εξάμπλ",
        "name_latin": "Anna Exampl",
    }

    load_single_email(gated, metadata(), reply)

    names = _names(gated)
    assert ("Άννα Εξάμπλ", "extraction_v2") in names
    assert ("Anna Exampl", "extraction_v2") in names


def test_a_v1_extraction_records_no_extraction_name(gated):
    load_single_email(gated, metadata(), V1_EXTRACTION)

    assert _names(gated) == set()


def test_without_person_identity_the_names_are_not_recorded(tmp_path):
    from src.store.schema import _gated_steps

    conn = create_database(str(tmp_path / "pre170.db"), gated=False)
    for name, step in _gated_steps():
        if name == extraction_fields.NAME:
            step(conn)

    assert load_single_email(conn, metadata(), v2_reply())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "person_identifiers" not in tables

"""The alias map: a mention row stands for the identity it names, by review, never by merge.

Proposals need two signals at once: the mention's name agrees with the identity's
in transliteration (a shared surname, no contradicting word), and the two rows
occur in the same thread. --apply records each as a sync_metadata row and changes
no people row. Every name and address below is invented.
"""

import sqlite3

import pytest

from src.store import person_aliases
from src.store.person_aliases import apply_aliases, propose_aliases
from src.store.person_resolve import ALIAS_PREFIX, resolve_identity
from src.store.schema import create_database

IDENTITY = "brostopoulou.evi@example.com"


@pytest.fixture(autouse=True)
def _no_owner(monkeypatch):
    monkeypatch.setattr("src.config.USER_NAME", "")
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "")


def _person(conn, name, email=None):
    return conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email)).lastrowid


def _email(conn, thread):
    n = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] + 1
    return conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, conversation_id) "
        "VALUES (?, '2026-09-01T10:00:00', ?, ?)",
        (n, f"subject {n}", thread),
    ).lastrowid


def _link(conn, email_id, *person_ids):
    for pid in person_ids:
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'to')",
            (email_id, pid),
        )


def _store(conn=None):
    conn = conn or create_database(":memory:")
    ids = {
        "identity": _person(conn, "E. Brostopoulou (Ευανθία)", IDENTITY),
        "mention": _person(conn, "Evi Brostopoulou"),
        "elsewhere": _person(conn, "Μπροστοπούλου Ευανθία"),
    }
    # The identity and the first mention write in one thread, on different emails;
    # the second mention appears only in a thread the identity never joined.
    _link(conn, _email(conn, "thread-a"), ids["identity"])
    _link(conn, _email(conn, "thread-a"), ids["mention"])
    _link(conn, _email(conn, "thread-b"), ids["elsewhere"])
    conn.commit()
    return conn, ids


def _aliases(conn):
    return dict(
        conn.execute(
            "SELECT key, value FROM sync_metadata WHERE key LIKE ? ORDER BY key",
            (ALIAS_PREFIX + "%",),
        ).fetchall()
    )


def test_a_mention_sharing_a_thread_and_the_surname_is_proposed():
    conn, ids = _store()

    plan = propose_aliases(conn)

    assert [(p["mention_id"], p["identity_id"]) for p in plan.proposals] == [
        (ids["mention"], ids["identity"])
    ]
    assert plan.proposals[0]["shared_threads"] == 1


def test_a_mention_on_the_same_email_is_proposed_without_a_thread():
    conn = create_database(":memory:")
    identity = _person(conn, "E. Brostopoulou (Ευανθία)", IDENTITY)
    mention = _person(conn, "Μπροστοπούλου Ευανθία")
    _link(conn, _email(conn, None), identity, mention)

    assert [p["mention_id"] for p in propose_aliases(conn).proposals] == [mention]


def test_an_address_initial_that_is_the_forename_covers_no_other_surname():
    """At t.kalogirou@ for 'Theano Kalogirou' the 't' is Theano: it must not stand for
    the surname of a 'Theodoridou Theano' who writes in the same thread."""
    conn = create_database(":memory:")
    identity = _person(conn, "Theano Kalogirou", "t.kalogirou@example.com")
    mention = _person(conn, "Theodoridou Theano")
    _link(conn, _email(conn, "thread-a"), identity, mention)

    assert propose_aliases(conn).proposals == []


def test_a_contradicting_forename_is_never_proposed():
    conn, ids = _store()
    maria = _person(conn, "Maria Brostopoulou")
    _link(conn, _email(conn, "thread-a"), maria)

    assert maria not in [p["mention_id"] for p in propose_aliases(conn).proposals]


@pytest.mark.parametrize(("shared", "proposed"), [(1, False), (2, True)])
def test_a_one_word_mention_needs_half_its_threads_shared(shared, proposed):
    """A row named 'Evanthia' gathers every Evanthia the model read: one thread in
    common is not enough to say it is this one."""
    conn = create_database(":memory:")
    identity = _person(conn, "E. Brostopoulou (Ευανθία)", IDENTITY)
    mention = _person(conn, "Evanthia")
    for n in range(3):
        email_id = _email(conn, f"thread-{n}")
        _link(conn, email_id, mention, *([identity] if n < shared else []))

    plan = propose_aliases(conn)

    assert ([p["mention_id"] for p in plan.proposals] == [mention]) is proposed
    assert plan.unshared == (0 if proposed else 1)


def test_two_identities_that_fit_make_the_mention_ambiguous():
    conn, ids = _store()
    other = _person(conn, "Evi Brostopoulou-Kalogirou", "evi.bk@example.org")
    _link(conn, _email(conn, "thread-a"), other)

    plan = propose_aliases(conn)

    assert ids["mention"] not in [p["mention_id"] for p in plan.proposals]
    assert plan.ambiguous == 1


def test_apply_records_aliases_and_changes_no_person():
    conn, ids = _store()
    people = conn.execute("SELECT * FROM people ORDER BY id").fetchall()
    links = conn.execute("SELECT * FROM email_people ORDER BY email_id, person_id").fetchall()

    written = apply_aliases(conn, propose_aliases(conn).proposals)

    assert written == 1
    assert _aliases(conn) == {f"{ALIAS_PREFIX}{ids['mention']}": str(ids["identity"])}
    assert conn.execute("SELECT * FROM people ORDER BY id").fetchall() == people
    assert (
        conn.execute("SELECT * FROM email_people ORDER BY email_id, person_id").fetchall() == links
    )
    res = resolve_identity(conn, "Evi Brostopoulou")
    assert (res.row["email"], res.resolved_by) == (IDENTITY, "alias")


def test_an_aliased_mention_is_not_proposed_again():
    conn, _ = _store()
    apply_aliases(conn, propose_aliases(conn).proposals)

    assert propose_aliases(conn).proposals == []


def _file_store(tmp_path):
    db = tmp_path / "brain.db"
    conn, ids = _store(create_database(str(db)))
    conn.close()
    return db, ids


def _file_aliases(db):
    conn = sqlite3.connect(db)
    try:
        return _aliases(conn)
    finally:
        conn.close()


def test_the_entry_point_dry_runs_by_default(tmp_path, capsys):
    db, _ = _file_store(tmp_path)

    assert person_aliases.main(["--db", str(db)]) == 0
    assert person_aliases.main(["--db", str(db), "--dry-run"]) == 0

    assert _file_aliases(db) == {}
    assert capsys.readouterr().out


def test_the_entry_point_applies_when_asked(tmp_path):
    db, ids = _file_store(tmp_path)

    assert person_aliases.main(["--db", str(db), "--apply"]) == 0

    assert _file_aliases(db) == {f"{ALIAS_PREFIX}{ids['mention']}": str(ids["identity"])}


def test_apply_is_refused_on_a_replica(tmp_path, monkeypatch):
    db, _ = _file_store(tmp_path)
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert person_aliases.main(["--db", str(db), "--apply"]) == 2
    assert _file_aliases(db) == {}


def test_a_missing_store_is_an_error(tmp_path):
    assert person_aliases.main(["--db", str(tmp_path / "absent.db")]) == 2
    assert not (tmp_path / "absent.db").exists()

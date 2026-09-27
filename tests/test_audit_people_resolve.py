"""resolve_person matches a name at the start of a word, and an address's local part.

It matched the folded query anywhere inside a name, so 'AI' resolved to a
forename holding it and 'EU' to a bank, and recall attached that dossier to
topic queries (search-3). A name written only in Greek could not be found by the Latin
surname its address spells either. Synthetic people only.
"""

from src.store.context import resolve_person
from src.store.schema import create_database


def _person(conn, name, email=None, links=0):
    pid = conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email)).lastrowid
    for _ in range(links):
        email_id = conn.execute(
            "INSERT INTO emails (message_id, date_received) VALUES (?, '2026-09-01T10:00:00')",
            (conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] + 1,),
        ).lastrowid
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'cc')",
            (email_id, pid),
        )
    return pid


def _store():
    conn = create_database(":memory:")
    _person(conn, "Νίκος (Nikolaidis)", "nikolaidis.example@example.com", links=5)
    _person(conn, "Zeus Holdings GR", "holdings@example.org", links=3)
    _person(conn, "Theodoros Example", "t.example@example.com", links=2)
    _person(conn, "ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ", "papadopoulou.marina@example.com", links=1)
    _person(conn, "Μαρία Συνθέτου", "msynthetou@example.com", links=4)
    return conn


def _resolved(conn, query):
    row, _count, _others = resolve_person(conn, query)
    return row["name"] if row else None


def test_a_query_inside_a_word_matches_no_one():
    conn = _store()
    assert _resolved(conn, "AI") is None
    assert _resolved(conn, "EU") is None
    assert resolve_person(conn, "aid") == (None, 0, [])


def test_a_query_at_the_start_of_a_word_still_matches():
    conn = _store()
    assert _resolved(conn, "Nikolaidis") == "Νίκος (Nikolaidis)"
    assert _resolved(conn, "Theo") == "Theodoros Example"
    assert _resolved(conn, "Παπαδοπούλου") == "ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ"
    assert _resolved(conn, "zeus holdings") == "Zeus Holdings GR"


def test_the_local_part_of_an_address_finds_a_greek_name():
    conn = _store()
    # 'msynthetou': an initial glued to the surname.
    assert _resolved(conn, "Synthetou") == "Μαρία Συνθέτου"
    # A token of the local part, and its start.
    assert _resolved(conn, "marina") == "ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ"


def test_the_local_part_needs_four_letters():
    conn = _store()
    # Three letters after an initial would fit many addresses.
    assert resolve_person(conn, "syn") == (None, 0, [])
    assert _resolved(conn, "synt") == "Μαρία Συνθέτου"


def test_every_match_is_counted_and_the_runners_up_named():
    conn = _store()
    _person(conn, "Example Other", "other@example.net")

    row, count, others = resolve_person(conn, "example")

    # By name twice, and by the local part of nikolaidis.example@: the most-emailed wins.
    assert row["name"] == "Νίκος (Nikolaidis)"
    assert count == 3
    assert [o["name"] for o in others] == ["Theodoros Example", "Example Other"]

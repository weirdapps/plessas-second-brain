"""Which names are the owner's: BRAIN_USER_NAME in any script, order, case or title.

The owner was 64 people rows, his address, an alias address, bounce rewrites and
dozens of spellings of his name with and without titles, so a question about him
split across rows (data-quality-05). One rule says which spellings are his, and
src/store/person_resolve.py maps all of them to one row. Names are invented.
"""

import pytest

from src.store.owner import is_owner_name, owner_person_id
from src.store.schema import create_database

OWNER = "Theano Okapidou"


def _person(conn, name, email, links):
    pid = conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email)).lastrowid
    for _ in range(links):
        n = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] + 1
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received) VALUES (?, ?, '2026-09-01')",
            (n, n),
        )
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'to')",
            (n, pid),
        )
    return pid


def test_the_owner_row_is_the_busiest_of_the_rows_at_his_addresses():
    conn = create_database(":memory:")
    primary = _person(conn, "T. Okapidou", "owner@example.com", 2)
    alias = _person(conn, "Okapidou Theano", "alias@example.org", 5)

    assert owner_person_id(conn, [primary, alias]) == alias


def test_a_name_alone_never_makes_a_row_the_owner():
    """A display name is anyone's to write; an address under the owner's control is not.
    With no row at one of his addresses there is no owner row, however the busiest
    row named 'Theano Okapidou' is spelled."""
    conn = create_database(":memory:")
    _person(conn, "Theano Okapidou", "theano@example.net", 50)

    assert owner_person_id(conn, []) is None


@pytest.mark.parametrize(
    "name",
    [
        "Theano Okapidou",
        "Okapidou Theano",
        "OKAPIDOU",
        "Οκαπίδου",
        "Θεανώ Οκαπίδου",
        "κα Οκαπίδου",
        "Mrs Okapidou",
        "T. Okapidou",
        "Okapidou T",
        "Θ. Οκαπίδου",
    ],
)
def test_a_spelling_of_the_owners_name_is_his(name):
    assert is_owner_name(name, OWNER) is True


@pytest.mark.parametrize(
    "name",
    [
        "Danae Okapidou",  # another forename
        "D. Okapidou",  # another initial
        "Theano Okapidis",  # another surname
        "Theano",  # a forename alone is anyone's
        "Okapidou Team",  # more than a name
        "",
    ],
)
def test_another_name_is_not_his(name):
    assert is_owner_name(name, OWNER) is False


def test_no_configured_name_matches_no_one():
    assert is_owner_name("Theano Okapidou", "") is False
    assert is_owner_name("Theano Okapidou", None) is False


def test_a_greek_forename_form_and_its_colloquial_form_are_both_his():
    """Greek forenames come in a formal and a colloquial form that share a stem;
    a feminine form of the same stem is someone else."""
    owner = "Grigorios Okapidis"

    assert is_owner_name("Γρηγόρης Οκαπίδης", owner) is True
    assert is_owner_name("Grigoris Okapidi", owner) is True  # colloquial, and a vocative
    assert is_owner_name("Grigori Okapidis", owner) is True
    assert is_owner_name("Grigoria Okapidi", owner) is False

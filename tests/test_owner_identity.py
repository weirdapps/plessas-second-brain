"""Which names are the owner's: BRAIN_USER_NAME in any script, order, case or title.

The owner was 64 people rows, his address, an alias address, bounce rewrites and
dozens of spellings of his name with and without titles, so a question about him
split across rows (data-quality-05). One rule says which spellings are his, and
src/store/person_resolve.py maps all of them to one row. Names are invented.
"""

import pytest

from src.store.owner import is_owner_name

OWNER = "Theano Okapidou"


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

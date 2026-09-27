"""A resolved MRI links to its person once the person exists.

person_id was looked up only when the MRI was resolved, and a 'resolved' row is
never revisited, so a colleague seen in Teams before any email created their
people row stayed unlinked for good: 21 senders and 305 messages on the replica
(teams-calendar-9). Synthetic people only.
"""

from unittest.mock import patch

import pytest

from src.extract.teams_mri import resolve_mris
from tests.teams.test_teams_mri import _seed_chat_with_message, _seed_person, _seed_resolution


def test_a_resolved_mri_links_to_a_person_created_later(db):
    _seed_chat_with_message(db, mri="8:orgid:aaaa-1111")
    # Resolved while no people row held the address.
    _seed_resolution(db, "8:orgid:aaaa-1111", None, email="Novak@Example.com")
    person_id = _seed_person(db, email="novak@example.com")

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        mock.side_effect = lambda *a, **k: pytest.fail("a resolved MRI was fetched again")
        resolve_mris(db)

    cache = db.execute("SELECT person_id FROM teams_mri_resolution").fetchone()
    assert cache["person_id"] == person_id
    msg = db.execute("SELECT sender_person_id FROM teams_messages").fetchone()
    assert msg["sender_person_id"] == person_id


def test_a_resolved_mri_with_no_person_stays_unlinked(db):
    _seed_chat_with_message(db, mri="8:orgid:aaaa-1111")
    _seed_resolution(db, "8:orgid:aaaa-1111", None, email="stranger@example.com")
    _seed_person(db, email="novak@example.com")

    with patch("src.extract.teams_mri.run_teams_cli"):
        resolve_mris(db)

    assert db.execute("SELECT person_id FROM teams_mri_resolution").fetchone()[0] is None
    assert db.execute("SELECT sender_person_id FROM teams_messages").fetchone()[0] is None

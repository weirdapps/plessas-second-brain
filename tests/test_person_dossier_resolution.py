"""The dossiers say how their person was found, and recall attaches only a sure one.

person_context, sender_brief and meeting_prep carry resolved_by (email, mri, jid,
name or alias), match_count and other_candidates with each candidate's identifier
kind, so a caller can tell an exact address from a guess among namesakes. recall's
own dossier comes only for an identifier or a name that fits one person. Every name
and address below is invented.
"""

import pytest

from src.bridge import sender_brief
from src.store.context import get_person_context
from src.store.person_resolve import ALIAS_PREFIX
from src.store.query import meeting_prep
from src.store.recall import recall
from src.store.schema import create_database

IDENTITY = "brostopoulou.evi@example.com"


@pytest.fixture(autouse=True)
def _no_owner(monkeypatch):
    monkeypatch.setattr("src.config.USER_NAME", "")
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "")


@pytest.fixture
def conn():
    c = create_database(":memory:")
    people = [
        ("E. Brostopoulou (Ευανθία)", IDENTITY, 3),
        ("Μπροστοπούλου Ευανθία (Evi Brostopoulou)", None, 6),
        ("Evi Brostopoulou", None, 4),
    ]
    n = 0
    for name, email, links in people:
        pid = c.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email)).lastrowid
        for _ in range(links):
            n += 1
            c.execute(
                "INSERT INTO emails (id, message_id, date_received, subject, summary) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day'), ?, 'A note')",
                (n, n, f"Subject {n}"),
            )
            c.execute(
                "INSERT INTO email_people (email_id, person_id, role_in_email) "
                "VALUES (?, ?, 'sender')",
                (n, pid),
            )
    c.commit()
    return c


def _alias_the_mentions(conn):
    identity = conn.execute("SELECT id FROM people WHERE email = ?", (IDENTITY,)).fetchone()[0]
    for (mention,) in conn.execute("SELECT id FROM people WHERE email IS NULL").fetchall():
        conn.execute(
            "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
            (f"{ALIAS_PREFIX}{mention}", str(identity)),
        )
    conn.commit()


def test_person_context_names_how_the_person_was_found(conn):
    by_name = get_person_context(conn, "Evi Brostopoulou")
    by_address = get_person_context(conn, IDENTITY)
    nobody = get_person_context(conn, "Nobody Anywhere")

    assert by_name["person"]["email"] == IDENTITY
    assert (by_name["resolved_by"], by_name["match_count"]) == ("name", 3)
    assert [c["identifier"] for c in by_name["other_candidates"]] == [None, None]
    assert by_name["email_count"] == 3
    assert (by_address["resolved_by"], by_address["match_count"]) == ("email", 1)
    assert (nobody["person"], nobody["resolved_by"], nobody["match_count"]) == (None, None, 0)


def test_sender_brief_passes_the_resolution_on(conn):
    brief = sender_brief(conn, "Μπροστοπούλου")

    assert brief["email"] == IDENTITY
    assert (brief["resolved_by"], brief["match_count"]) == ("name", 3)
    assert brief["other_candidates"]


def test_meeting_prep_says_how_each_attendee_was_found(conn):
    attendees = meeting_prep(conn, ["Evi Brostopoulou", IDENTITY, "Nobody Anywhere"])["attendees"]

    assert [a.get("resolved_email") for a in attendees] == [IDENTITY, IDENTITY, None]
    assert [a["resolved_by"] for a in attendees] == ["name", "email", None]
    assert [a["match_count"] for a in attendees] == [3, 1, 0]


def test_recall_attaches_no_dossier_for_a_name_several_rows_fit(conn):
    result = recall(conn, "Μπροστοπούλου", include_context=True)

    assert result["person_context"] is None
    assert result["summary"]["has_person_context"] is False


def test_recall_attaches_the_dossier_for_an_address(conn):
    result = recall(conn, IDENTITY, include_context=True)

    assert result["person_context"]["person"]["email"] == IDENTITY
    assert result["person_context"]["resolved_by"] == "email"


def test_recall_attaches_the_dossier_once_the_aliases_make_the_name_one_person(conn):
    _alias_the_mentions(conn)

    result = recall(conn, "Μπροστοπούλου", include_context=True)

    assert result["person_context"]["person"]["email"] == IDENTITY
    assert result["person_context"]["match_count"] == 1

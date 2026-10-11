"""A name resolves to an identity first, in either script, and says how sure it is.

The people table mixes identities (a row with an address, a Teams MRI or a
WhatsApp JID) with the mentions extraction made of names in text, which have
none. A name matched by folded substring landed on a mention as often as not: a
direct report's dossier said "0 Teams messages, no meetings" because the
mention named 'Firstname Surname' was found while the addressed row, written
'F. Surname', held the history; and the Latin and the Greek spelling of one
person attached different dossiers (retrieval-quality-02, consumers-03,
code-store-11). Every name and address below is invented.
"""

import json
import sqlite3

import pytest

from src.store.context import resolve_person
from src.store.person_resolve import ALIAS_PREFIX, resolve_identity
from src.store.schema import create_database

IDENTITY = "brostopoulou.evi@example.com"


@pytest.fixture(autouse=True)
def _no_owner(monkeypatch):
    """No owner configured unless a test sets one: a developer's own would leak in."""
    monkeypatch.setattr("src.config.USER_NAME", "")
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "")


def _person(conn, name, email=None, links=0, thread=None):
    pid = conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email)).lastrowid
    for _ in range(links):
        n = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] + 1
        email_id = conn.execute(
            "INSERT INTO emails (message_id, date_received, conversation_id) "
            "VALUES (?, '2026-09-01T10:00:00', ?)",
            (n, thread or f"thread-{n}"),
        ).lastrowid
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'cc')",
            (email_id, pid),
        )
    return pid


def _store():
    """One colleague as the store holds her: an addressed row named by an initial,
    and two address-less mentions, each more emailed than the identity."""
    conn = create_database(":memory:")
    ids = {
        "identity": _person(conn, "E. Brostopoulou (Ευανθία)", IDENTITY, links=3),
        "mention_gr": _person(conn, "Μπροστοπούλου Ευανθία (Evi Brostopoulou)", links=6),
        "mention_en": _person(conn, "Evi Brostopoulou", links=4),
    }
    conn.commit()
    return conn, ids


def _resolved_email(conn, query, **kwargs):
    res = resolve_identity(conn, query, **kwargs)
    return res.row["email"] if res.row is not None else None


class TestIdentitiesFirst:
    @pytest.mark.parametrize(
        "query",
        [
            "Μπροστοπούλου",
            "Brostopoulou",
            "Mprostopoulou",
            "Brostopulu",
            "ΜΠΡΟΣΤΟΠΟΥΛΟΥ",
            "Ευανθία Μπροστοπούλου",
            "Evanthia Brostopoulou",
            "Brostopoulou Evanthia",
            "Evi Brostopoulou",
            "Εύη Μπροστοπούλου",
            "κα Μπροστοπούλου",
        ],
    )
    def test_every_spelling_resolves_to_the_addressed_row(self, query):
        conn, _ = _store()

        assert _resolved_email(conn, query) == IDENTITY
        assert _resolved_email(conn, query, local_part=False) == IDENTITY

    def test_an_identity_outranks_a_more_emailed_mention(self):
        conn, _ = _store()

        res = resolve_identity(conn, "Brostopoulou")

        assert res.row["email"] == IDENTITY
        assert res.resolved_by == "name"
        assert res.match_count == 3
        assert [c["identifier"] for c in res.other_candidates] == [None, None]

    def test_a_mention_only_match_hands_over_to_the_identity_sharing_the_surname(self):
        conn, _ = _store()

        # 'Evi Brostopoulou' is written out only by the mentions; the identity's
        # initial covers 'Evi' and its surname is the same in transliteration.
        res = resolve_identity(conn, "Evi Brostopoulou")

        assert res.row["email"] == IDENTITY
        assert res.resolved_by == "name"
        assert res.match_count == 3

    def test_a_stray_identity_spelling_the_name_loses_to_the_busy_one_that_fits(self):
        # An Exchange-wrapped address on one email spelled the full name; the
        # person's own row, 'E. Surname', held the history.
        conn, _ = _store()
        _person(conn, "Evi Brostopoulou", "imceaex-evi@example.net", links=1)

        res = resolve_identity(conn, "Evi Brostopoulou")

        assert res.row["email"] == IDENTITY
        assert res.other_candidates[0]["email"] == "imceaex-evi@example.net"

    def test_an_initial_the_row_spells_out_stands_for_that_name_only(self):
        conn, _ = _store()
        _person(conn, "K. Kalogirou", "kalogirou@example.org", links=1)

        # 'E. Brostopoulou (Ευανθία)' at brostopoulou.evi@ is Evanthia: not Eleni.
        assert resolve_identity(conn, "Eleni Brostopoulou").row is None
        # A bare initial may be anyone with it.
        assert _resolved_email(conn, "Katerina Kalogirou") == "kalogirou@example.org"

    def test_a_contradicting_forename_is_not_handed_over(self):
        conn, _ = _store()
        _person(conn, "Maria Brostopoulou", links=1)

        res = resolve_identity(conn, "Maria Brostopoulou")

        assert res.row["name"] == "Maria Brostopoulou"
        assert res.row["email"] is None
        assert res.match_count == 1

    def test_a_name_no_row_spells_takes_an_identity_only_when_nothing_contradicts(self):
        conn = create_database(":memory:")
        _person(conn, "E. Brostopoulou (Ευανθία)", IDENTITY, links=1)

        evi = resolve_identity(conn, "Evi Brostopoulou")

        assert (evi.row["email"], evi.resolved_by, evi.match_count) == (IDENTITY, "name", 1)
        assert resolve_identity(conn, "Anna Brostopoulou").row is None

    def test_a_query_of_another_script_finds_a_name_stored_in_one(self):
        conn = create_database(":memory:")
        _person(conn, "Ξυπνητίδης Ορέστης", "o.x@example.com", links=1)
        _person(conn, "Orestis Xypnitidis", "orestis@example.org", links=2)

        greek = resolve_identity(conn, "Ksipnitidis")
        latin = resolve_identity(conn, "Ξυπνητίδης")

        assert greek.match_count == latin.match_count == 2
        assert greek.row["email"] == latin.row["email"] == "orestis@example.org"


class TestResolvedBy:
    def test_an_address_resolves_by_email(self):
        conn, _ = _store()

        res = resolve_identity(conn, IDENTITY.upper())

        assert (res.row["email"], res.resolved_by, res.match_count) == (IDENTITY, "email", 1)

    def test_an_unknown_address_resolves_to_no_one(self):
        conn, _ = _store()

        res = resolve_identity(conn, "nobody@example.com")

        assert (res.row, res.match_count, res.other_candidates, res.resolved_by) == (
            None,
            0,
            [],
            None,
        )

    def test_a_whatsapp_jid_resolves_by_jid(self):
        conn = create_database(":memory:")
        _person(conn, "Lefki Kalogirou", "30000000001@s.whatsapp.net")

        res = resolve_identity(conn, "30000000001@s.whatsapp.net")

        assert (res.row["name"], res.resolved_by) == ("Lefki Kalogirou", "jid")

    def test_a_teams_mri_resolves_by_mri_and_makes_an_identity(self):
        conn = create_database(":memory:")
        pid = _person(conn, "Ioli Pervanidou", links=1)
        _person(conn, "Ioli P.", links=5)
        conn.execute(
            "INSERT INTO teams_mri_resolution (mri, display_name, person_id, status) "
            "VALUES ('8:orgid:0000-ioli', 'Pervanidou, Ioli', ?, 'resolved')",
            (pid,),
        )

        by_mri = resolve_identity(conn, "8:orgid:0000-ioli")
        by_name = resolve_identity(conn, "Ioli")

        assert (by_mri.row["id"], by_mri.resolved_by) == (pid, "mri")
        # The MRI makes the row an identity, so it outranks the busier mention.
        assert by_name.row["id"] == pid
        assert by_name.other_candidates == [{"name": "Ioli P.", "email": None, "identifier": None}]

    def test_empty_and_wildcard_queries_resolve_to_no_one(self):
        conn, _ = _store()

        for query in ("", "  ", "%", "_", ".", "κα"):
            assert resolve_identity(conn, query).row is None


class TestAmbiguousForename:
    def test_a_forename_alone_resolves_to_the_busiest_identity_and_counts_the_rest(self):
        conn = create_database(":memory:")
        _person(conn, "Nefeli Kalogirou", "n.kalogirou@example.com", links=5)
        _person(conn, "Νεφέλη Ζαχαρίου", "nzachariou@example.com", links=2)
        _person(conn, "Nefeli", links=9)

        res = resolve_identity(conn, "Nefeli")

        assert res.row["email"] == "n.kalogirou@example.com"
        assert res.resolved_by == "name"
        assert res.match_count == 3
        assert res.other_candidates == [
            {"name": "Νεφέλη Ζαχαρίου", "email": "nzachariou@example.com", "identifier": "email"},
            {"name": "Nefeli", "email": None, "identifier": None},
        ]


class TestAliasMap:
    def _alias(self, conn, mention_id, identity_id):
        conn.execute(
            "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
            (f"{ALIAS_PREFIX}{mention_id}", str(identity_id)),
        )

    def test_an_aliased_mention_stands_for_its_identity(self):
        conn, ids = _store()
        self._alias(conn, ids["mention_gr"], ids["identity"])
        self._alias(conn, ids["mention_en"], ids["identity"])

        by_alias = resolve_identity(conn, "Evi Brostopoulou")
        by_name = resolve_identity(conn, "Μπροστοπούλου")

        assert (by_alias.row["email"], by_alias.resolved_by, by_alias.match_count) == (
            IDENTITY,
            "alias",
            1,
        )
        assert (by_name.row["email"], by_name.resolved_by, by_name.match_count) == (
            IDENTITY,
            "name",
            1,
        )

    def test_an_alias_to_a_row_that_is_gone_is_ignored(self):
        conn, ids = _store()
        self._alias(conn, ids["mention_en"], 999999)

        res = resolve_identity(conn, "Evi Brostopoulou")

        assert res.row["email"] == IDENTITY
        assert res.match_count == 3


class TestOwner:
    @pytest.fixture
    def owner(self, monkeypatch):
        monkeypatch.setattr("src.config.USER_NAME", "Theano Okapidou")
        monkeypatch.setattr(
            "src.config.USER_EMAIL_PATTERN", "owner@example.com, t.okapidou@example.net"
        )
        conn = create_database(":memory:")
        ids = {
            "owner": _person(conn, "Okapidou Theano", "owner@example.com", links=8),
            "alias_address": _person(conn, "T. Okapidou", "t.okapidou@example.net", links=4),
            "honorific": _person(conn, "κα Οκαπίδου", links=12),
            "greek": _person(conn, "Θεανώ Οκαπίδου", links=3),
            "relative": _person(conn, "Danae Okapidou", "danae@example.org", links=2),
            # Anyone can be named 'Theano Okapidou': this address is not one she lists.
            "impostor": _person(conn, "Theano Okapidou", "theano@example.org", links=20),
        }
        conn.commit()
        return conn, ids

    @pytest.mark.parametrize(
        ("query", "how"),
        [
            ("Okapidou", "alias"),
            ("Οκαπίδου", "alias"),
            ("Θεανώ Οκαπίδου", "alias"),
            ("Theano Okapidou", "alias"),
            ("OKAPIDOU T.", "alias"),
            ("κα Οκαπίδου", "alias"),
            ("owner@example.com", "email"),
            ("T.Okapidou@example.net", "email"),
        ],
    )
    def test_every_owner_spelling_and_listed_address_maps_to_the_owner_row(self, owner, query, how):
        conn, ids = owner

        res = resolve_identity(conn, query)

        assert (res.row["id"], res.resolved_by) == (ids["owner"], how)

    def test_an_address_he_does_not_list_is_someone_else_whatever_its_name(self, owner):
        conn, ids = owner

        res = resolve_identity(conn, "theano@example.org")

        assert (res.row["id"], res.resolved_by) == (ids["impostor"], "email")

    def test_the_owner_counts_once_however_many_rows_spell_him(self, owner):
        conn, ids = owner

        res = resolve_identity(conn, "Okapidou")

        # The owner (four rows), the relative and the impostor: three people.
        assert res.match_count == 3
        assert [c["email"] for c in res.other_candidates] == [
            "theano@example.org",
            "danae@example.org",
        ]

    def test_a_relative_with_the_surname_is_not_the_owner(self, owner):
        conn, ids = owner

        res = resolve_identity(conn, "Danae Okapidou")

        assert (res.row["id"], res.resolved_by) == (ids["relative"], "name")

    def test_with_no_row_at_his_addresses_a_name_pins_no_one(self, owner, monkeypatch):
        conn, ids = owner
        monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "elsewhere@example.com")

        res = resolve_identity(conn, "Theano Okapidou")

        # An ordinary name lookup: the busiest identity so named, by name.
        assert (res.row["id"], res.resolved_by) == (ids["impostor"], "name")
        assert res.match_count > 1


class TestCompatibility:
    def test_resolve_person_keeps_its_three_values(self):
        conn, _ = _store()

        row, count, others = resolve_person(conn, "Brostopoulou")

        assert row["email"] == IDENTITY
        assert count == 3
        assert all(set(o) == {"name", "email", "identifier"} for o in others)
        assert resolve_person(conn, "") == (None, 0, [])


def test_a_file_store_finds_a_person_added_after_the_first_lookup(tmp_path):
    """The words of every name are indexed once per store and reused; a change to
    the people table must reach the next lookup on any connection."""
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    _person(conn, "Orestis Xypnitidis", "orestis@example.org", links=1)
    conn.commit()
    assert resolve_identity(conn, "Ksipnitidis").match_count == 1

    other = sqlite3.connect(db)
    other.execute("INSERT INTO people (name, email) VALUES ('Ορέστης Ξυπνητίδης', NULL)")
    other.commit()
    other.close()

    assert resolve_identity(conn, "Ksipnitidis").match_count == 2


def test_the_alias_map_is_read_from_sync_metadata_rows():
    """No migration: an alias is a sync_metadata row, so a replica gets it by the pull."""
    conn, ids = _store()
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
        (f"{ALIAS_PREFIX}{ids['mention_en']}", json.dumps(ids["identity"])),
    )

    assert resolve_identity(conn, "Evi Brostopoulou").match_count == 2

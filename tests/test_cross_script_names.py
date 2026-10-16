"""A name typed in Latin letters finds it written in Greek, and the reverse
(src/store/cross_script.py, src/store/query.py)."""

import pytest

from src.store.cross_script import name_forms
from src.store.query import _sanitize_fts5_query, query_by_keyword, search_attachments
from src.store.schema import create_database
from tests import factories
from tests.patching import unset_env


@pytest.fixture(autouse=True)
def _names_on(monkeypatch):
    unset_env(monkeypatch, "BRAIN_QUERY_NAMES")


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    factories.person(conn, "Νικόλαος Οκαπίδης", "nokapidis@example.com")
    factories.person(conn, "Maria Zebrou", "mzebrou@example.com")
    factories.person(conn, "Μαρία Ζέμπρου", None)
    return conn


def test_a_latin_name_word_has_its_greek_forms(store):
    forms = name_forms(store)

    assert forms.alternatives("Okapidis") == ("οκαπιδησ",)
    assert forms.alternatives("OKAPIDIS") == ("οκαπιδησ",)


def test_a_greek_name_word_has_its_latin_forms(store):
    assert name_forms(store).alternatives("Ζέμπρου") == ("zebrou",)


def test_words_no_name_holds_have_none(store):
    forms = name_forms(store)

    for word in ("budget", "κάρτες", "the", "Q3", "2026", "ok"):
        assert forms.alternatives(word) == ()


def test_the_query_holds_the_other_script_as_an_alternative(store):
    expression = _sanitize_fts5_query("Okapidis budget", names=name_forms(store))

    name, rest = expression.split(") AND ")
    assert name.startswith('("Okapidis" OR "οκαπιδ')  # the Greek form, by its stem if any
    assert rest == '"budget"'


def test_no_names_builds_the_query_as_before():
    assert _sanitize_fts5_query("Okapidis budget") == '"Okapidis" "budget"'


def test_keyword_search_finds_greek_mail_by_a_latin_name(store):
    email_id = factories.email(
        store,
        subject="Προϋπολογισμός 2027",
        summary="Ο Οκαπίδης ενέκρινε το budget.",
        content="Σώμα.",
    )

    rows = query_by_keyword(store, "Okapidis budget", limit=5)

    assert [r["email_id"] for r in rows] == [email_id]
    assert not rows[0].get("partial_match")  # every word held, in either script


def test_keyword_search_finds_latin_mail_by_a_greek_name(store):
    email_id = factories.email(
        store, subject="Pricing", summary="Ms Zebrou approved the pricing.", content="Body."
    )

    rows = query_by_keyword(store, "Ζέμπρου pricing", limit=5)

    assert [r["email_id"] for r in rows] == [email_id]


def test_attachment_search_takes_the_names_too(store):
    attachment_id = factories.attachment(
        store, filename="minutes.pdf", text="Παρόντες: Οκαπίδης, Ζέμπρου."
    )

    rows = search_attachments(store, "Okapidis minutes", limit=5)

    assert [r["attachment_id"] for r in rows] == [attachment_id]
    assert rows[0].get("partial_match")  # 'minutes' is in no text: partial, but found


def test_the_switch_turns_it_off(store, monkeypatch):
    monkeypatch.setenv("BRAIN_QUERY_NAMES", "0")
    factories.email(store, subject="Θέμα", summary="Ο Οκαπίδης ενέκρινε.", content="Σώμα.")

    assert name_forms(store) is None
    assert query_by_keyword(store, "Okapidis", limit=5) == []


def test_the_forms_are_built_once_per_store_until_people_change(store):
    first = name_forms(store)
    assert name_forms(store) is first

    factories.person(store, "Γιώργος Λεοπαρδάκης", None)

    again = name_forms(store)
    assert again is not first
    assert again.alternatives("Leopardakis") == ("λεοπαρδακησ",)


def test_a_store_without_people_has_no_forms():
    conn = create_database(":memory:")

    assert name_forms(conn).alternatives("Okapidis") == ()

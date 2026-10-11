"""Greek query-side stemming: a word finds the other forms of that word, and only them.

Greek inflects every noun, adjective and verb, and keyword search matched the form
typed: on one store "καρτες" was in 3,966 emails and some form of the word in 13,243,
so a search for κάρτες missed κάρτα and καρτών, and nothing said so. A Greek query
word of four letters or more is now also searched by its Snowball stem, as a prefix
term, when the stem is a shorter prefix of the word and has four letters or more.
Every word is still required. BRAIN_QUERY_STEMMING=0 restores exact words.

The words are ordinary Greek words; every document is synthetic.
"""

import logging
import sys

import pytest

from src.store.greek import PHRASE_MATCH, query_stem, sb_match_args
from src.store.query import (
    _sanitize_fts5_query,
    fts5_phrase,
    fts5_query_variants,
    query_by_keyword,
)
from src.store.schema import create_database

# One document per form: a query for any form should find the family's every form.
FAMILIES = (
    ("τράπεζα", "τράπεζας", "τράπεζες", "τραπεζών"),
    ("πληρωμή", "πληρωμής", "πληρωμές", "πληρωμών"),
    ("κάρτα", "κάρτας", "κάρτες", "καρτών"),
    ("έγκριση", "έγκρισης", "εγκρίσεις", "εγκρίσεων"),
    ("πελάτης", "πελάτη", "πελάτες", "πελατών"),
    ("συνάντηση", "συνάντησης", "συναντήσεις", "συναντήσεων"),
)

# Different words whose first four letters or more agree: a stem must not join them,
# in either direction.
NEGATIVES = (
    (("πελάτης", "πελάτες"), ("πέλαγος", "πελάγη")),
    (("σύμβαση", "συμβάσεις"), ("σύμβολο", "σύμβολα")),
    (("απόφαση", "αποφάσεις"), ("αποφυγή", "αποφυγής")),
    (("κατάθεση", "καταθέσεις"), ("κατάθλιψη", "κατάθλιψης")),
    (("προσφορά", "προσφορές"), ("προσφυγή", "προσφυγές")),
    (("συνάντηση", "συναντήσεις"), ("συναίνεση", "συναίνεσης")),
    (("ευρώ",), ("Ευρώπη", "Ευρώπης")),
)


def _store(words) -> tuple:
    """A store with one email per word, its summary holding that word alone among
    words of the fixture; returns the connection and each word's email id."""
    conn = create_database(":memory:")
    ids = {}
    for n, word in enumerate(dict.fromkeys(words), start=1):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (?, ?, '2026-09-01T10:00:00Z', ?, ?)",
            (n, n, f"Ενημέρωση {n}", f"Σημείωση: {word}"),
        )
        ids[word] = n
    conn.commit()
    return conn, ids


@pytest.fixture
def corpus():
    words = [w for family in FAMILIES for w in family]
    words += [w for pair in NEGATIVES for side in pair for w in side]
    conn, ids = _store(words)
    yield conn, ids
    conn.close()


@pytest.fixture
def exact(monkeypatch):
    monkeypatch.setenv("BRAIN_QUERY_STEMMING", "0")


def _found(conn, query: str) -> set[int]:
    return {row["email_id"] for row in query_by_keyword(conn, query, limit=200)}


def _recall(conn, ids, family) -> list[float]:
    """For each form of `family` as the query, the share of the family's emails found."""
    docs = {ids[word] for word in family}
    return [len(_found(conn, word) & docs) / len(docs) for word in family]


# --- the fixture: every form found, and nothing else joined -------------------------


@pytest.mark.parametrize("family", FAMILIES)
def test_any_form_of_a_word_finds_every_form(corpus, family):
    conn, ids = corpus

    assert _recall(conn, ids, family) == [1.0] * len(family)


def test_the_fixture_recall_rises_from_the_form_typed_to_every_form(corpus, monkeypatch):
    conn, ids = corpus

    def mean_recall():
        shares = [share for family in FAMILIES for share in _recall(conn, ids, family)]
        return sum(shares) / len(shares)

    monkeypatch.setenv("BRAIN_QUERY_STEMMING", "0")
    before = mean_recall()
    monkeypatch.setenv("BRAIN_QUERY_STEMMING", "1")
    after = mean_recall()

    assert before == pytest.approx(0.25)
    assert after == 1.0


@pytest.mark.parametrize("left, right", NEGATIVES)
def test_words_that_only_share_their_first_letters_stay_apart(corpus, left, right):
    conn, ids = corpus

    for query_side, other_side in ((left, right), (right, left)):
        others = {ids[word] for word in other_side}
        for word in query_side:
            assert not _found(conn, word) & others, word


def test_stemming_is_on_unless_switched_off():
    # conftest clears BRAIN_QUERY_STEMMING, so this is the default.
    assert query_stem("κάρτες") == "καρτ"


def test_the_switch_restores_the_form_typed(corpus, exact):
    conn, ids = corpus

    for family in FAMILIES:
        assert _recall(conn, ids, family) == [1 / len(family)] * len(family)
    assert query_stem("κάρτες") is None
    assert _sanitize_fts5_query("κάρτες") == '"καρτες"'
    assert fts5_phrase("πιστωτικές κάρτες") == '"πιστωτικες καρτες"'
    assert sb_match_args("κάρτες") == ("καρτεσ", "", "")


# --- the guards ---------------------------------------------------------------------


def test_a_greek_word_is_its_typed_form_or_its_stem_as_a_prefix():
    assert query_stem("Κάρτες;") == "καρτ"
    assert _sanitize_fts5_query("κάρτες") == '("καρτες" OR "καρτ"*)'


def test_a_word_of_fewer_than_four_letters_is_searched_as_typed():
    assert query_stem("ώρα") is None
    assert _sanitize_fts5_query("ώρα") == '"ωρα"'


def test_a_stem_of_fewer_than_four_letters_is_not_a_prefix():
    # τιμή stems to τιμ-, which also starts τιμολόγιο.
    assert query_stem("τιμές") is None
    assert _sanitize_fts5_query("τιμές") == '"τιμες"'


def test_a_stem_that_does_not_start_the_word_is_not_used():
    # Snowball changes letters in a few words: κόνιτσα stems to κονακ-, shorter than
    # the word and long enough, but a prefix of no form of it; αλέστε to αλιεστ-.
    assert query_stem("κόνιτσα") is None
    assert query_stem("αλέστε") is None


def test_a_word_the_stemmer_leaves_whole_is_searched_as_typed():
    # σχεδόν has no ending to drop; as a prefix it would only add longer words.
    assert query_stem("σχεδόν") is None


def test_a_stopword_is_never_stemmed():
    # A query of stopwords alone keeps them in the full-text match, as typed.
    assert query_stem("σχετικά") is None
    assert _sanitize_fts5_query("σχετικά με") == '"σχετικα" "με"'


def test_latin_words_codes_and_numbers_follow_the_existing_rules():
    for word in ("payments", "cards", "Q4", "12345", "κάρτα2", "e-κάρτα"):
        assert query_stem(word) is None, word
    assert _sanitize_fts5_query("payments cards") == '"payments" "cards"'
    assert _sanitize_fts5_query("what is the ACME") == '"ACME"'


def test_a_mixed_query_requires_every_word_with_an_explicit_and():
    # FTS5 reads two phrases side by side as AND, but not a phrase and a group.
    assert _sanitize_fts5_query("Revolut κάρτες") == '"Revolut" AND ("καρτες" OR "καρτ"*)'


# --- every word is still required ---------------------------------------------------


@pytest.fixture
def two_cards():
    conn = create_database(":memory:")
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (?, ?, '2026-09-01T10:00:00Z', ?, ?)",
        [
            (1, 1, "Ενημέρωση", "Η κάρτα εκδόθηκε"),
            (2, 2, "Ενημέρωση", "Πληρωμές με κάρτα"),
        ],
    )
    conn.commit()
    yield conn
    conn.close()


def test_every_word_is_still_required(two_cards):
    rows = query_by_keyword(two_cards, "κάρτες πληρωμή")

    assert [row["email_id"] for row in rows] == [2]
    assert not rows[0].get("partial_match")


def test_the_any_word_fallback_uses_the_stems_too(two_cards):
    rows = query_by_keyword(two_cards, "κάρτες ανύπαρκτηλέξη")

    assert {row["email_id"] for row in rows} == {1, 2}
    assert all(row["partial_match"] for row in rows)
    assert [partial for _expression, partial in fts5_query_variants("κάρτες ανύπαρκτηλέξη")] == [
        False,
        True,
    ]


# --- sb_match uses the same stems ---------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
def test_every_row_the_index_finds_is_a_whole_match_for_sb_match(corpus, family):
    conn, _ids = corpus

    for word in family:
        rows = query_by_keyword(conn, word, limit=200)
        assert rows, word
        for row in rows:
            (score,) = conn.execute(
                "SELECT sb_match(?, ?, ?, ?)", (row["summary"], *sb_match_args(word))
            ).fetchone()
            assert score >= PHRASE_MATCH, (word, row["summary"])


def test_the_recall_buckets_find_every_form():
    from src.store.recall import recall

    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (1, 1, '2026-09-01T10:00:00Z', 'Ενημέρωση', 'Σημείωση')"
    )
    conn.executemany(
        "INSERT INTO decisions (email_id, decision) VALUES (1, ?)",
        [("Εγκρίθηκε η νέα κάρτα",), ("Νέες χρεωστικές κάρτες",), ("Τέλη καρτών",), ("Άλλο θέμα",)],
    )
    conn.commit()

    out = recall(conn, "κάρτες")

    assert sorted(d["decision"] for d in out["decisions"]) == [
        "Εγκρίθηκε η νέα κάρτα",
        "Νέες χρεωστικές κάρτες",
        "Τέλη καρτών",
    ]
    assert not any(d.get("partial_match") for d in out["decisions"])


def test_sb_match_holds_every_word_by_its_stem():
    phrase, words, tokens = sb_match_args("πιστωτικές κάρτες")

    assert phrase == "πιστωτικεσ καρτεσ"
    assert words.split("\x1f") == ["πιστωτικ", "καρτ"]
    assert tokens.split("\x1f") == ["πιστωτικ", "καρτ"]


def test_two_forms_of_one_word_count_once():
    _phrase, words, tokens = sb_match_args("κάρτα κάρτες")

    assert words == "καρτ"
    assert tokens == "καρτ"


def test_the_snippet_marks_the_form_the_stem_found():
    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary, content) "
        "VALUES (1, 1, '2026-09-01T10:00:00Z', 'Ενημέρωση', 'Σημείωση', "
        "'Η νέα κάρτα εκδόθηκε σήμερα')"
    )
    conn.commit()

    rows = query_by_keyword(conn, "κάρτες")

    assert [row["source"] for row in rows] == ["content"]
    assert "<b>καρτα</b>" in rows[0]["snippet"]


# --- Teams and WhatsApp try the phrase first -----------------------------------------


def _teams_threads(conn, summaries):
    conn.execute(
        "INSERT INTO teams_chats (id, teams_chat_id, chat_kind, first_seen_at) "
        "VALUES (1, '19:x', 'channel', '2026-09-01')"
    )
    conn.executemany(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at, "
        "message_count, title, summary, extraction_status) "
        "VALUES (?, 1, 'channel_post', '2026-09-01', '2026-09-01', 1, 'Θέμα', ?, 'extracted')",
        list(enumerate(summaries, start=1)),
    )
    conn.commit()


def test_a_teams_word_finds_every_form_though_one_thread_holds_it_as_typed():
    from src.store.teams_query import search_teams

    conn = create_database(":memory:")
    _teams_threads(conn, ["Νέες κάρτες για το cashback", "Τέλη καρτών"])

    rows = search_teams(conn, "κάρτες")

    assert sorted(row["thread_id"] for row in rows) == [1, 2]
    assert not any(row.get("partial_match") for row in rows)


def test_a_teams_phrase_holds_in_any_inflection():
    from src.store.teams_query import search_teams

    conn = create_database(":memory:")
    _teams_threads(
        conn,
        ["Τέλη πιστωτικών καρτών", "Οι κάρτες και οι πιστωτικές γραμμές"],
    )

    rows = search_teams(conn, "πιστωτικές κάρτες")

    # The phrase, inflected, is tried before the words in any order.
    assert [row["thread_id"] for row in rows] == [1]


def test_a_phrase_without_a_stem_is_the_folded_query_quoted_whole():
    assert fts5_phrase("cashback 2026") == '"cashback 2026"'
    assert fts5_phrase("Τέλη ώρας") == '"Τελη ωρας"'


def test_a_phrase_with_a_stem_skips_bare_symbols_and_still_matches():
    # An FTS5 phrase element with no token in it ("-") matches nothing at all.
    from src.store.teams_query import search_teams

    conn = create_database(":memory:")
    _teams_threads(conn, ['Οι νέες κάρτες "Visa" για το 2026'])

    assert fts5_phrase('κάρτες "Visa" - 2026') == '"καρτ"* + """Visa""" + "2026"'
    assert [row["thread_id"] for row in search_teams(conn, 'κάρτες "Visa" - 2026')] == [1]


def test_a_whatsapp_word_finds_every_form():
    from src.store.whatsapp_query import search_whatsapp

    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    conn.executemany(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at, "
        "summary) VALUES (1, ?, '2026-09-01T10:00:00Z', '2026-09-01T11:00:00Z', ?)",
        [("m1", "Νέες κάρτες"), ("m2", "Η κάρτα ήρθε")],
    )
    conn.commit()

    rows = search_whatsapp(conn, "κάρτες")

    assert len(rows) == 2


# --- a checkout pulled without its dependencies --------------------------------------


@pytest.fixture
def no_stemmer(monkeypatch):
    from src.store import greek

    monkeypatch.setitem(sys.modules, "snowballstemmer", None)
    greek._snowball.cache_clear()
    yield
    greek._snowball.cache_clear()


def test_without_the_stemmer_installed_words_are_searched_as_typed(no_stemmer, caplog):
    conn, ids = _store(["κάρτα", "κάρτες"])

    with caplog.at_level(logging.WARNING, logger="src.store.greek"):
        found = _found(conn, "κάρτες")

    assert found == {ids["κάρτες"]}
    assert "snowballstemmer" in caplog.text

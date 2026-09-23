"""Name, topic and free-text matching must work the way agents actually ask.

Schema v20 folded accents for the FTS indexes only. Everything that matches an
entity by LIKE stayed accent-sensitive, and SQLite's LIKE folds ASCII case only,
so Greek capitals never matched lower case:

* topics are stored normalized (lower case, accents stripped), and six query
  sites compared an ACCENTED query against them: 0 hits for 7 of 7 common Greek
  words tried on the replica, 15-138 without accents;
* people are mostly stored in ALL-CAPS Greek, so a normal-case name found no one,
  and when several people matched, fetchone() picked an arbitrary one;
* the free-text buckets required every token (FTS), the exact phrase (Teams) or
  the whole query as one substring (decisions, actions, commitments), and agents
  write long mixed queries: roughly half of those came back empty.

The Greek strings below are synthetic.
"""

import pytest

from src.store.schema import create_database


@pytest.fixture
def conn():
    c = create_database(":memory:")
    c.executemany(
        "INSERT INTO people (id, name, email) VALUES (?, ?, ?)",
        [
            (1, "ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ", "m.papadopoulou@example.com"),
            (2, "ΠΑΠΑΔΟΠΟΥΛΟΥ Α.", "a.papadopoulou@example.com"),
            (3, "ΚΑΡΑΓΙΑΝΝΗΣ Ν.", "n.karagiannis@example.com"),
        ],
    )
    for i in range(1, 6):
        c.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (?, ?, '2026-09-01T10:00:00', ?, ?)",
            (i, i, f"Θέμα {i}", f"Περίληψη για το 2026 και τον προϋπολογισμό {i}"),
        )
    # Α. appears on four emails, Μαρίνα on one: Α. is the likelier Παπαδοπούλου.
    c.executemany(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'to')",
        [(1, 1), (2, 2), (3, 2), (4, 2), (5, 2), (1, 3)],
    )
    c.execute(
        "INSERT INTO topics (id, name, display_name) VALUES (1, 'προυπολογισμος 2027', 'Προϋπολογισμός 2027')"
    )
    c.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (1, 1)")
    c.execute(
        "INSERT INTO decisions (email_id, decision, decided_by, decision_date) "
        "VALUES (1, 'Εγκρίθηκε ο προϋπολογισμός καρτών', 'ΚΑΡΑΓΙΑΝΝΗΣ Ν.', '2026-09-01')"
    )
    c.execute(
        "INSERT INTO action_items (email_id, task, owner, status) "
        "VALUES (1, 'Ετοιμασία παρουσίασης για τις κάρτες', 'ΠΑΠΑΔΟΠΟΥΛΟΥ Α.', 'open')"
    )
    c.execute(
        "INSERT INTO action_items (email_id, task, owner, status) "
        "VALUES (2, 'Αποστολή του προϋπολογισμού καρτών', 'ΚΑΡΑΓΙΑΝΝΗΣ Ν.', 'open')"
    )
    c.commit()
    return c


def test_search_fold_matches_greek_case_accents_and_final_sigma():
    from src.store.greek import search_fold

    assert search_fold("ΠΑΠΑΔΟΠΟΥΛΟΣ") == search_fold("Παπαδόπουλος") == "παπαδοπουλοσ"
    assert search_fold("Προϋπολογισμός") == "προυπολογισμοσ"


def test_person_lookup_ignores_case_and_accents(conn):
    from src.store.context import get_person_context

    ctx = get_person_context(conn, "Καραγιάννης")

    assert ctx["person"]["name"] == "ΚΑΡΑΓΙΑΝΝΗΣ Ν."


def test_an_ambiguous_name_resolves_to_the_most_emailed_person_and_says_so(conn):
    from src.store.context import get_person_context

    ctx = get_person_context(conn, "παπαδοπουλου")

    assert ctx["person"]["name"] == "ΠΑΠΑΔΟΠΟΥΛΟΥ Α."
    assert ctx["match_count"] == 2
    assert [c["name"] for c in ctx["other_candidates"]] == ["ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ"]


def test_an_accented_topic_finds_the_normalized_topic(conn):
    from src.store.context import get_topic_context

    ctx = get_topic_context(conn, "Προϋπολογισμός")

    assert ctx["topic"] is not None


def test_query_decisions_person_filter_is_case_and_accent_blind(conn):
    from src.store.query import query_decisions

    rows = query_decisions(conn, person="Καραγιάννης", days=None)

    assert len(rows) == 1


def test_query_actions_owner_filter_is_case_and_accent_blind(conn):
    from src.store.query import query_action_items

    rows = query_action_items(conn, owner="παπαδοπούλου")

    assert len(rows) == 1


def test_recall_decisions_and_actions_match_without_accents(conn):
    from src.store.recall import recall

    out = recall(conn, "προυπολογισμος καρτων")

    assert len(out["decisions"]) == 1
    assert [a["task"] for a in out["actions"]] == ["Αποστολή του προϋπολογισμού καρτών"]


def test_rows_holding_the_whole_query_come_back_alone(conn):
    """A LIKE bucket that has rows with the whole query returns only those, never
    mixed with rows that hold one of its words."""
    from src.store.recall import recall

    conn.execute("INSERT INTO decisions (email_id, decision) VALUES (3, 'Μόνο για τις καρτών')")
    conn.commit()

    out = recall(conn, "προυπολογισμος καρτων")

    assert [d["decision"] for d in out["decisions"]] == ["Εγκρίθηκε ο προϋπολογισμός καρτών"]
    assert not any(d.get("partial_match") for d in out["decisions"])


def test_recall_falls_back_to_any_token_and_marks_it(conn):
    """No row carries every token, so the fallback returns rows with some, best
    first, flagged so the agent knows the match is partial."""
    from src.store.recall import recall

    out = recall(conn, "παρουσίαση κάρτες εξωτερικός ελεγκτής")

    assert len(out["actions"]) == 1
    assert out["actions"][0]["partial_match"] is True


def test_keyword_search_retries_with_any_token_when_every_token_misses(conn):
    from src.store.query import query_by_keyword

    exact = query_by_keyword(conn, "προϋπολογισμό")
    loose = query_by_keyword(conn, "προϋπολογισμό ανύπαρκτηλέξη")

    assert exact and not any(r.get("partial_match") for r in exact)
    assert loose and all(r["partial_match"] for r in loose)


def test_the_fallback_ignores_stopwords_short_tokens_and_numbers(conn):
    """A year or a function word in the fallback would match half the corpus:
    every test email carries "για", and none carries the made-up word."""
    from src.store.query import query_by_keyword

    assert query_by_keyword(conn, "ανύπαρκτηλέξη 2026 για το") == []


def _teams(conn):
    conn.execute(
        "INSERT INTO teams_chats (id, teams_chat_id, chat_kind, first_seen_at) "
        "VALUES (1, '19:x', 'channel', '2026-09-01')"
    )
    conn.execute(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at, message_count, "
        "title, summary, extraction_status) VALUES (1, 1, 'channel_post', '2026-09-01', "
        "'2026-09-01', 1, 'Κάρτες', 'Συζήτηση για τις χρεωστικές κάρτες και το cashback', 'extracted')"
    )
    conn.commit()


def test_teams_search_no_longer_needs_the_exact_phrase(conn):
    from src.store.teams_query import search_teams

    _teams(conn)

    rows = search_teams(conn, "cashback χρεωστικές")  # both words, other order

    assert len(rows) == 1
    assert not rows[0].get("partial_match")


def test_teams_search_falls_back_to_any_token(conn):
    from src.store.teams_query import search_teams

    _teams(conn)

    rows = search_teams(conn, "cashback ανύπαρκτηλέξη")

    assert len(rows) == 1
    assert rows[0]["partial_match"] is True


def test_each_free_text_bucket_reads_its_table_once(conn):
    """The folded match runs in Python on every row: on the replica one pass over
    decisions costs about 0.2 s and one over action_items about 0.3 s. A phrase
    pass followed by a token pass doubled that for every long query."""
    import re

    from src.store.recall import recall

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    recall(conn, "παρουσίαση κάρτες εξωτερικός ελεγκτής")
    conn.set_trace_callback(None)

    for table in ("decisions", "action_items", "commitments", "inline_images"):
        passes = [s for s in statements if re.search(rf"FROM {table}\b", s)]
        assert len(passes) == 1, (table, len(passes))


def test_recall_dates_an_undated_meeting_decision_by_its_meeting(conn):
    """A calendar decision has no date of its own when the model gave none, or
    gave free text that the loader drops. Ordered by decision_date alone it sank
    below every dated decision, however recent the meeting."""
    from src.store.recall import recall

    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "is_recurring, is_self_organized, is_cancelled, ingested_at, llm_status) VALUES "
        "(1, 'ev1', 'Επιτροπή', '2026-09-20T10:00:00', '2026-09-20T11:00:00', 0, 0, 0, "
        "'2026-09-20T12:00:00', 'extracted')"
    )
    conn.execute("INSERT INTO decisions (event_id, decision) VALUES (1, 'Νέα τιμολόγηση')")
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decision_date) "
        "VALUES (2, 'Παλιά τιμολόγηση', '2025-01-01')"
    )
    conn.commit()

    out = recall(conn, "τιμολόγηση")

    assert [d["decision"] for d in out["decisions"]] == ["Νέα τιμολόγηση", "Παλιά τιμολόγηση"]


def _calendar(conn):
    conn.executemany(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "is_recurring, is_self_organized, is_cancelled, ingested_at, llm_status) "
        "VALUES (?, ?, ?, ?, ?, 0, 0, 0, '2026-09-01T00:00:00', 'extracted')",
        [
            (10, "e10", "Επιτροπή καρτών", "2026-09-23T15:00:00", "2026-09-23T16:00:00"),
            (11, "e11", "Άλλη συνάντηση", "2026-09-24T09:00:00", "2026-09-24T10:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO event_attendees (event_id, email, name, response_status, is_organizer, "
        "is_self) VALUES (?, ?, ?, 'accepted', 0, 0)",
        [
            (10, "m.papadopoulou@example.com", "ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ"),
            (10, "a.papadopoulou@example.com", "ΠΑΠΑΔΟΠΟΥΛΟΥ Α."),
        ],
    )
    conn.commit()


def test_calendar_person_filter_is_case_and_accent_blind_and_lists_an_event_once(conn, monkeypatch):
    """Attendee names are mostly ALL-CAPS Greek and LOWER() folds ASCII only. And
    the filter was a JOIN, so an event came back once per matching attendee."""
    from src import mcp_server

    _calendar(conn)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.query_calendar_events(person="Παπαδοπούλου")

    assert [e["id"] for e in out["events"]] == [10]


def test_calendar_until_includes_its_own_day(conn, monkeypatch):
    """start_at carries a time, so start_at <= '2026-09-23' dropped every event
    on the 23rd."""
    from src import mcp_server

    _calendar(conn)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.query_calendar_events(since="2026-09-23", until="2026-09-23")

    assert [e["id"] for e in out["events"]] == [10]


# --------------------------------------------------------------- review round 2


def _decisions(conn, *texts):
    for text in texts:
        conn.execute("INSERT INTO decisions (email_id, decision) VALUES (5, ?)", (text,))
    conn.commit()


def test_a_token_matches_at_the_start_of_a_word_not_inside_one(conn):
    """'act' used to match 'contract': a substring anywhere in a word."""
    from src.store.recall import recall

    _decisions(conn, "Contract renewal signed", "Act on the audit findings")

    out = recall(conn, "act ανύπαρκτηλέξη")

    assert [d["decision"] for d in out["decisions"]] == ["Act on the audit findings"]


@pytest.mark.parametrize(
    "query",
    [
        '"προϋπολογισμός καρτών"',
        "“προϋπολογισμός καρτών”",
        "προϋπολογισμός καρτών;",
        "προϋπολογισμός καρτών?",
        # One word has no token pass: only the stripped phrase can match it.
        "«προϋπολογισμός»",
        "προϋπολογισμός;",
    ],
)
def test_quotes_and_question_marks_do_not_change_the_match(conn, query):
    """Punctuation stayed in the phrase, so a quoted or questioned query lost the
    whole-query tier and its real match came back flagged partial."""
    from src.store.recall import recall

    out = recall(conn, query)

    assert [d["decision"] for d in out["decisions"]] == ["Εγκρίθηκε ο προϋπολογισμός καρτών"]
    assert not out["decisions"][0].get("partial_match")


def test_a_two_letter_acronym_is_searched_and_question_words_are_not(conn):
    """'what did we decide about UX' fell back to what/did/decide/about and
    returned the lunch menu, never the UX decision."""
    from src.store.recall import recall

    _decisions(conn, "UX redesign approved", "Told them what the lunch menu is about")

    out = recall(conn, "what did we decide about UX")

    assert [d["decision"] for d in out["decisions"]] == ["UX redesign approved"]


def test_the_fallback_keeps_reference_numbers_but_not_years_or_small_words():
    from src.store.greek import search_tokens

    assert search_tokens("invoice 4500123457 2026 Q4 ab to") == ["invoice", "4500123457", "q4"]


def test_decomposed_text_folds_like_composed_text():
    """Text from PDFs and macOS arrives decomposed (alpha + combining acute);
    the FTS tokenizer folds it and the LIKE side did not."""
    import unicodedata

    from src.store.greek import search_fold

    composed = "κάρτες πληρωμών"
    assert search_fold(unicodedata.normalize("NFD", composed)) == search_fold(composed)


def test_registering_twice_mid_iteration_is_harmless(conn):
    from src.store.greek import register_sql_functions

    cursor = conn.execute("SELECT id FROM people")
    cursor.fetchone()

    register_sql_functions(conn)  # already registered: must not raise

    assert cursor.fetchone() is not None


def test_a_name_filter_folds_each_person_once_not_each_email_link(conn):
    """Joined through email_people, sb_fold could run once per link: without
    planner statistics (the store never runs ANALYZE) SQLite scanned the link
    table first, 1.3M calls and 2 s or more for any name. People are folded in a
    pass of their own, whatever plan the email query gets."""
    from src.store.greek import register_sql_functions, search_fold
    from src.store.query import query_by_person, query_combined

    _dated_emails(conn)
    register_sql_functions(conn)
    calls = []

    def counting(text):
        calls.append(text)
        return search_fold(text)

    conn.create_function("sb_fold", 1, counting, deterministic=True)
    people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    for search in (query_by_person, lambda c, n: query_combined(c, person=n)):
        calls.clear()
        search(conn, "Καραγιάννης")
        assert len(calls) == people


@pytest.mark.parametrize(
    "lookup",
    [
        lambda c: __import__("src.store.query", fromlist=["x"]).query_by_topic(c, "Προϋπολογισμός"),
        lambda c: __import__("src.store.query", fromlist=["x"]).query_decisions(
            c, topic="Προϋπολογισμός", days=None
        ),
        lambda c: __import__("src.store.query", fromlist=["x"]).query_combined(
            c, topic="Προϋπολογισμός"
        ),
        lambda c: __import__("src.store.query", fromlist=["x"]).meeting_prep(
            c, ["Καραγιάννης"], topic="Προϋπολογισμός", days=100000
        )["topic_context"]["decisions"],
        lambda c: __import__("src.store.recall", fromlist=["x"]).recall(c, "Προϋπολογισμός")[
            "topic_context"
        ],
    ],
    ids=["query_by_topic", "query_decisions", "query_combined", "meeting_prep", "recall"],
)
def test_every_topic_lookup_finds_the_normalized_topic(conn, lookup):
    assert lookup(conn)


@pytest.mark.parametrize(
    "lookup",
    [
        lambda c: __import__("src.store.query", fromlist=["x"]).query_by_person(c, "Καραγιάννης"),
        lambda c: __import__("src.store.query", fromlist=["x"]).query_combined(
            c, person="Καραγιάννης"
        ),
        lambda c: __import__("src.store.query", fromlist=["x"]).meeting_prep(
            c, ["Καραγιάννης"], days=100000
        )["attendees"][0]["emails"],
    ],
    ids=["query_by_person", "query_combined", "meeting_prep"],
)
def test_every_person_lookup_ignores_case_and_accents(conn, lookup):
    assert lookup(conn)


def test_meeting_prep_resolves_a_namesake_to_one_person_and_says_so(conn):
    """It merged every Παπαδοπούλου into one dossier; person_context picks the
    most-emailed and names the others, and so does this now."""
    from src.store.query import meeting_prep

    dossier = meeting_prep(conn, ["Παπαδοπούλου"], days=100000)["attendees"][0]

    assert dossier["match_count"] == 2
    assert [c["name"] for c in dossier["other_candidates"]] == ["ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ"]
    assert len(dossier["emails"]) == 4


def test_sender_brief_passes_the_ambiguity_on(conn):
    from src.bridge import sender_brief

    brief = sender_brief(conn, "Παπαδοπούλου")

    assert brief["match_count"] == 2
    assert brief["other_candidates"]


def test_match_count_counts_past_the_candidates_shown(conn):
    from src.store.context import get_person_context

    conn.executemany(
        "INSERT INTO people (name, email) VALUES (?, ?)",
        [(f"ΠΑΠΑΔΟΠΟΥΛΟΥ {n}", f"p{n}@example.com") for n in ("Β.", "Γ.", "Δ.")],
    )
    conn.commit()

    assert get_person_context(conn, "παπαδοπουλου")["match_count"] == 5


def test_recall_summary_names_the_kinds_that_matched_only_partly(conn):
    from src.store.recall import recall

    out = recall(conn, "παρουσίαση κάρτες εξωτερικός ελεγκτής")

    assert "actions" in out["summary"]["partial_kinds"]


def test_a_recalled_meeting_decision_carries_its_date_and_meeting(conn):
    """It was ordered by the meeting's date but arrived with no date and no
    subject, so the caller could not say where it came from."""
    from src.store.recall import recall

    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "is_recurring, is_self_organized, is_cancelled, ingested_at, llm_status) VALUES "
        "(7, 'ev7', 'Επιτροπή τιμολόγησης', '2026-09-20T10:00:00', '2026-09-20T11:00:00', "
        "0, 0, 0, '2026-09-20T12:00:00', 'extracted')"
    )
    conn.execute("INSERT INTO decisions (event_id, decision) VALUES (7, 'Νέα τιμολόγηση')")
    conn.commit()

    row = recall(conn, "τιμολόγηση")["decisions"][0]

    assert (row["date"], row["email_subject"], row["source"]) == (
        "2026-09-20T10:00:00",
        "Επιτροπή τιμολόγησης",
        "calendar",
    )


def test_an_undated_teams_decision_is_dated_by_its_thread(conn):
    from src.store.recall import recall

    _teams(conn)
    conn.execute("UPDATE teams_threads SET started_at = '2026-09-22T09:00:00' WHERE id = 1")
    conn.execute("INSERT INTO decisions (teams_thread_id, decision) VALUES (1, 'Νέα χρέωση')")
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decision_date) "
        "VALUES (2, 'Παλιά χρέωση', '2025-01-01')"
    )
    conn.commit()

    out = recall(conn, "χρέωση")

    assert [d["decision"] for d in out["decisions"]] == ["Νέα χρέωση", "Παλιά χρέωση"]


def test_conversation_and_attachment_searches_fall_back_too(conn):
    from src.store.conversation_query import search_conversations_keyword
    from src.store.query import search_attachments

    conn.execute(
        "INSERT INTO conversations (id, session_id, started_at, created_at, summary) "
        "VALUES (1, 's1', '2026-09-01', '2026-09-01', 'Συζήτηση για τον προϋπολογισμό')"
    )
    conn.execute(
        "INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at) "
        "VALUES (1, 1, 1, 'plan.pdf', '/tmp/plan.pdf', '2026-09-01')"
    )
    conn.execute(
        "INSERT INTO attachment_content (id, attachment_id, extracted_text, extraction_status) "
        "VALUES (1, 1, 'Σχέδιο προϋπολογισμού για τις κάρτες', 'extracted')"
    )
    conn.commit()

    convs = search_conversations_keyword(conn, "προϋπολογισμό ανύπαρκτηλέξη")
    atts = search_attachments(conn, "κάρτες ανύπαρκτηλέξη")

    assert convs and all(c["partial_match"] for c in convs)
    assert atts and all(a["partial_match"] for a in atts)


def test_calendar_until_with_a_time_is_taken_literally(conn, monkeypatch):
    from src import mcp_server

    _calendar(conn)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.query_calendar_events(since="2026-09-23", until="2026-09-23T12:00:00")

    assert out["events"] == []


def test_calendar_keyword_falls_back_to_any_token(conn, monkeypatch):
    from src import mcp_server

    _calendar(conn)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.query_calendar_events(keyword="Επιτροπή ανύπαρκτηλέξη")

    assert [e["id"] for e in out["events"]] == [10]
    assert out["events"][0]["partial_match"] is True


def test_image_search_ignores_case_and_accents(conn, monkeypatch):
    from src import mcp_server

    conn.execute(
        "INSERT INTO inline_images (sha256, classification, classification_method, "
        "classified_at, vision_description, width, height, bytes) VALUES ('abc', 'content', "
        "'vision', '2026-09-01', 'Γράφημα με τις κάρτες πληρωμών', 800, 600, 1024)"
    )
    conn.commit()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.attachment_image_search("ΓΡΑΦΗΜΑ καρτες")

    assert [r["sha256"] for r in out["results"]] == ["abc"]
    assert not out["results"][0].get("partial_match")


def test_image_search_flags_a_partial_match(conn, monkeypatch):
    from src import mcp_server

    _image(conn, "abc", "Γράφημα με τις κάρτες πληρωμών")
    conn.commit()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.attachment_image_search("γράφημα ανύπαρκτηλέξη")

    assert out["results"][0]["partial_match"] is True


def test_person_context_finds_meetings_by_person_address_or_name(conn):
    """Attendee rows are matched by resolved person, by address (any case), and
    by folded name only for a person with no address on record."""
    from src.store.context import get_person_context

    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "is_recurring, is_self_organized, is_cancelled, ingested_at, llm_status) VALUES "
        "(20, 'ev20', 'Παλιά σύσκεψη', '2020-01-01T10:00:00', '2020-01-01T11:00:00', "
        "0, 0, 0, '2020-01-01T12:00:00', 'extracted'), "
        "(21, 'ev21', 'Μελλοντική σύσκεψη', '2099-01-01T10:00:00', '2099-01-01T11:00:00', "
        "0, 0, 0, '2020-01-01T12:00:00', 'extracted')"
    )
    conn.execute("INSERT INTO people (id, name) VALUES (9, 'ΔΗΜΗΤΡΙΟΥ Κ.')")
    conn.executemany(
        "INSERT INTO event_attendees (event_id, email, name, response_status, is_organizer, "
        "is_self) VALUES (?, ?, ?, 'accepted', 0, 0)",
        [
            (20, "N.Karagiannis@Example.com", "Karagiannis N"),
            (21, "k.dimitriou@elsewhere.example", "Δημητρίου Κ."),
        ],
    )
    conn.commit()

    by_address = get_person_context(conn, "Καραγιάννης")
    by_name = get_person_context(conn, "Δημητρίου")

    assert by_address["last_met"]["subject"] == "Παλιά σύσκεψη"
    assert by_name["next_meeting"]["subject"] == "Μελλοντική σύσκεψη"


def test_the_workspace_filter_applies_before_the_limit(conn):
    """Filtered after LIMIT, a page of matches from other workspaces came back
    empty and pushed the caller onto the any-token fallback."""
    from src.store.conversation_query import search_conversations_keyword

    for i in range(3):
        conn.execute(
            "INSERT INTO conversations (session_id, started_at, created_at, workspace, summary) "
            "VALUES (?, '2026-09-01', '2026-09-01', '/work/other', 'προϋπολογισμός καρτών')",
            (f"o{i}",),
        )
    conn.execute(
        "INSERT INTO conversations (session_id, started_at, created_at, workspace, summary) "
        "VALUES ('mine', '2026-08-01', '2026-08-01', '/work/brain', 'προϋπολογισμός καρτών')"
    )
    conn.commit()

    rows = search_conversations_keyword(conn, "προϋπολογισμός καρτών", workspace="brain", limit=1)

    assert [r["session_id"] for r in rows] == ["mine"]
    assert not rows[0].get("partial_match")


def test_the_cli_says_when_no_email_held_every_word(tmp_path, capsys):
    import types

    from src import cli

    db = tmp_path / "brain.db"
    store = create_database(str(db))
    store.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (1, 1, '2026-09-01T10:00:00', 'Θέμα', 'Περίληψη για τον προϋπολογισμό')"
    )
    store.commit()
    store.close()

    cli.cmd_query_keyword(
        types.SimpleNamespace(db=db, keyword="προϋπολογισμό ανύπαρκτηλέξη", limit=5, verbose=False)
    )

    assert "No email held every word" in capsys.readouterr().out


def _image(conn, sha, description):
    conn.execute(
        "INSERT INTO inline_images (sha256, classification, classification_method, "
        "classified_at, vision_description, width, height, bytes) VALUES (?, 'content', "
        "'vision', '2026-09-01', ?, 800, 600, 1024)",
        (sha, description),
    )


@pytest.mark.parametrize(
    ("table", "search"),
    [
        ("decisions", "_search_decisions"),
        ("action_items", "_search_actions"),
        ("commitments", "_search_commitments"),
        ("inline_images", "_search_inline_images"),
    ],
    ids=["decisions", "actions", "commitments", "images"],
)
def test_each_row_is_scored_once(conn, table, search):
    """Selected from a plain subquery, SQLite flattened it and scored every
    matching row a second time for the sort key: a Python call per row."""
    from src.store import recall
    from src.store.greek import _match_score

    conn.execute(
        "INSERT INTO commitments (email_id, commitment) VALUES (1, 'Θα στείλω τον προϋπολογισμό καρτών')"
    )
    _image(conn, "img1", "Πίνακας προϋπολογισμού καρτών")
    conn.commit()
    calls = []

    def counting(text, phrase, words, tokens):
        calls.append(text)
        return _match_score(text, phrase, words, tokens)

    conn.create_function("sb_match", 4, counting, deterministic=True)
    rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    getattr(recall, search)(conn, "προυπολογισμος καρτων", 5)

    assert len([c for c in calls if c]) == rows  # the empty one is the registration probe


# --------------------------------------------------------------- review round 3


def test_a_one_word_query_matches_at_the_start_of_a_word(conn):
    """The whole query was a substring anywhere, so 'AI' matched 'email' and
    'said', and 'UX' matched 'Luxembourg', all as whole-query hits. An acronym
    must end with its word too, or 'UX' is the start of 'UXBRIDGE'."""
    from src.store.recall import recall

    _decisions(
        conn,
        "Update the email templates",
        "AI pilot approved",
        "Said again: maintain the plan",
        "Luxembourg branch closes",
        "UXBRIDGE plans",
        "UX redesign approved",
        "Contract renewal signed",
    )

    assert [d["decision"] for d in recall(conn, "AI")["decisions"]] == ["AI pilot approved"]
    assert [d["decision"] for d in recall(conn, "UX")["decisions"]] == ["UX redesign approved"]
    assert recall(conn, "act")["decisions"] == []


def test_an_acronym_token_is_the_whole_word(conn):
    from src.store.recall import recall

    _decisions(conn, "UXBRIDGE plans", "UX redesign approved")

    out = recall(conn, "UX ανύπαρκτηλέξη")

    assert [d["decision"] for d in out["decisions"]] == ["UX redesign approved"]


@pytest.mark.parametrize("query", ["καρτών προϋπολογισμός", "καρτών και προϋπολογισμός"])
def test_every_word_in_another_order_is_a_whole_match(conn, query):
    """The instructions say keyword search wants every word; a row that holds
    them all, in any order, is not a partial match. A stopword is not one of
    the words it must hold."""
    from src.store.recall import recall

    out = recall(conn, query)

    assert [d["decision"] for d in out["decisions"]] == ["Εγκρίθηκε ο προϋπολογισμός καρτών"]
    assert not out["decisions"][0].get("partial_match")
    assert "decisions" not in out["summary"]["partial_kinds"]


def test_a_row_without_the_year_is_not_a_whole_match(conn):
    """A year is no search token, since it would match half the corpus, but it
    is still a word of the query: holding every other word does not make a row
    whole, or 2026's budget answers a question about 2027's."""
    from src.store.recall import recall

    _decisions(conn, "Εγκρίθηκε ο προϋπολογισμός 2026")

    out = recall(conn, "προϋπολογισμός 2027")

    assert out["decisions"]
    assert all(d.get("partial_match") for d in out["decisions"])
    assert "decisions" in out["summary"]["partial_kinds"]


def test_every_word_with_its_year_in_another_order_is_a_whole_match(conn):
    """A year is no search token, but a row holding it and every other word, in
    any order, holds the whole query, as the full-text buckets already say."""
    from src.store.recall import recall

    _decisions(
        conn,
        "The 2026 budget was approved",
        "Budget freeze for travel",
        "Budget line 20261",
        "Budget 1.2026 approved",
    )

    out = recall(conn, "budget 2026")

    assert [d["decision"] for d in out["decisions"]] == ["The 2026 budget was approved"]
    assert not out["decisions"][0].get("partial_match")


def test_partial_rows_come_back_most_tokens_first(conn):
    from src.store.recall import recall

    conn.executemany(
        "INSERT INTO decisions (email_id, decision, decision_date) VALUES (5, ?, ?)",
        [("New cards for the auditor", "2026-01-01"), ("New cards", "2026-09-10")],
    )
    conn.commit()

    out = recall(conn, "cards auditor zzzqqq")

    assert [d["decision"] for d in out["decisions"]] == ["New cards for the auditor", "New cards"]
    assert all(d["partial_match"] for d in out["decisions"])


def test_a_one_letter_article_is_not_a_word_the_row_must_hold(conn):
    from src.store.recall import recall

    out = recall(conn, "η αποστολή του προϋπολογισμού")

    assert [a["task"] for a in out["actions"]] == ["Αποστολή του προϋπολογισμού καρτών"]
    assert not out["actions"][0].get("partial_match")


def test_a_short_lowercase_word_is_still_a_word_of_the_query(conn):
    from src.store.recall import recall

    _decisions(conn, "Strategy for AI approved", "Strategy for the Thai market")

    out = recall(conn, "ai strategy")

    assert [d["decision"] for d in out["decisions"]] == ["Strategy for AI approved"]
    assert not out["decisions"][0].get("partial_match")


def test_a_phrase_ending_in_an_acronym_ends_with_it(conn):
    """The whole-word rule looked at the length of the whole phrase, so 'digital
    EU' ran into 'Digital Europe' and came back as a whole match."""
    from src.store.recall import recall

    _decisions(conn, "Digital Europe plan", "EU digital agenda")

    out = recall(conn, "digital EU")

    assert [d["decision"] for d in out["decisions"]] == ["EU digital agenda"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [("500", ["500 euros"]), ("50", ["50 cards"]), ("12345 ανύπαρκτηλέξη", ["ref 12345"])],
)
def test_a_number_matches_only_the_whole_number(conn, query, expected):
    """500 matched 5000, 1.500 and the Greek 500.000, 50 matched 50.000, and a
    reference number matched every longer one it began."""
    from src.store.recall import recall

    _decisions(
        conn,
        "5000 euros",
        "500 euros",
        "500.000 πελάτες",
        "1.500 ευρώ",
        "2,500 euros",
        "50 cards",
        "50.000 cards",
        "ref 12345",
        "ref 1234567",
    )

    assert [d["decision"] for d in recall(conn, query)["decisions"]] == expected


def test_a_term_with_a_symbol_keeps_it_in_a_longer_query(conn):
    from src.store.recall import recall

    _decisions(conn, "Plan C, developer hired", "C# developer hired")

    out = recall(conn, "C# developer")

    assert [d["decision"] for d in out["decisions"]] == ["C# developer hired"]


@pytest.mark.parametrize("query", ['"C"', "C."])
def test_a_quoted_letter_is_still_the_letter(conn, query):
    from src.store.recall import recall

    _decisions(conn, "Plan C approved")

    assert [d["decision"] for d in recall(conn, query)["decisions"]] == ["Plan C approved"]


@pytest.mark.parametrize(
    ("query", "kept", "dropped"),
    [
        ("Series A funding", "Series A funding closed", "Series B funding closed"),
        ("Basel I rules", "Basel I rules apply", "Basel III rules apply"),
    ],
)
def test_a_capital_letter_names_a_variant(conn, query, kept, dropped):
    """'a' and 'i' are stopwords as an article and a pronoun, not as the letter
    of Series A or Basel I."""
    from src.store.recall import recall

    _decisions(conn, kept, dropped)

    assert [d["decision"] for d in recall(conn, query)["decisions"]] == [kept]


def test_a_lowercase_article_is_not_a_word_the_row_must_hold(conn):
    from src.store.recall import recall

    _decisions(conn, "Digital euro pilot approved")

    out = recall(conn, "what is a digital euro")

    assert [d["decision"] for d in out["decisions"]] == ["Digital euro pilot approved"]
    assert not out["decisions"][0].get("partial_match")


@pytest.mark.parametrize(
    ("query", "row"),
    [("card cards", "New cards issued"), ("e-banking banking", "E-banking relaunch")],
)
def test_a_word_inside_another_word_of_the_query_still_counts(conn, query, row):
    """Counted in one pass, the longer word hid the shorter one it contains, and
    a row holding every word came back partial."""
    from src.store.recall import recall

    _decisions(conn, row)

    out = recall(conn, query)

    assert [d["decision"] for d in out["decisions"]] == [row]
    assert not out["decisions"][0].get("partial_match")


def test_only_a_letter_keeps_a_trailing_hash():
    """'C#' is a name; 'ticket#' is a word with a stray sign. As a search token
    'c#' would reach the full-text index as the letter c, which every row holds."""
    from src.store.greek import search_tokens, search_words

    assert search_words("C# ticket#") == ["c#", "ticket"]
    assert search_tokens("C# developer") == ["developer"]


def test_a_query_of_stopwords_only_keeps_them_in_the_full_text_match():
    from src.store.query import _sanitize_fts5_query

    assert _sanitize_fts5_query("what is the") == '"what" "is" "the"'
    assert _sanitize_fts5_query("what is the ACME") == '"ACME"'


def test_a_symbol_between_words_is_not_a_word(conn):
    from src.store.recall import recall

    _decisions(conn, "AI and ML roadmap")

    out = recall(conn, "AI + ML")

    assert [d["decision"] for d in out["decisions"]] == ["AI and ML roadmap"]
    assert not out["decisions"][0].get("partial_match")


def test_the_full_text_buckets_do_not_require_stopwords(conn):
    """A question holds words no row does ('what', 'the'), so the full-text
    buckets flagged every answer partial while the others called it whole."""
    from src.store.query import query_by_keyword

    rows = query_by_keyword(conn, "what about the προϋπολογισμό")

    assert rows and not any(r.get("partial_match") for r in rows)


def test_a_term_with_a_symbol_is_not_its_bare_letter(conn):
    """Stripping '#' from 'C#' left 'c', which matched 'Plan C' as the whole query."""
    from src.store.recall import recall

    _decisions(conn, "Plan C approved", "C# migration planned")

    assert [d["decision"] for d in recall(conn, "C#")["decisions"]] == ["C# migration planned"]


def test_a_verbatim_phrase_with_inner_punctuation_is_a_whole_match(conn):
    """'Q4:' is the word Q4, so the row holds every word of the query."""
    from src.store.recall import recall

    _decisions(conn, "Q4: 2027 budget approved", "Budget moved to Q1")

    out = recall(conn, "Q4: 2027 budget")

    assert [d["decision"] for d in out["decisions"]] == ["Q4: 2027 budget approved"]
    assert not out["decisions"][0].get("partial_match")


def test_partial_kinds_reads_the_hybrid_email_bucket(conn):
    """The MCP recall mixes keyword rows with semantic ones, which carry no
    partial flag either way: they must not hide that no email held the whole
    query, nor on their own claim that one held part of it."""
    from src.store.recall import recall

    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (9, 9, '2026-09-02T10:00:00', 'Άλλο θέμα', 'Τίποτα σχετικό')"
    )

    def semantic(c, q, k):
        return [9]

    partial = recall(conn, "προϋπολογισμό ανύπαρκτηλέξη", semantic_candidates=semantic)
    whole = recall(conn, "προϋπολογισμό", semantic_candidates=semantic)
    semantic_only = recall(conn, "ανύπαρκτηλέξη", semantic_candidates=semantic)

    assert 9 in [e["email_id"] for e in partial["emails"]]
    assert "emails" in partial["summary"]["partial_kinds"]
    assert "emails" not in whole["summary"]["partial_kinds"]
    assert [e["email_id"] for e in semantic_only["emails"]] == [9]
    assert "emails" not in semantic_only["summary"]["partial_kinds"]


def test_two_letter_function_words_are_stopwords_even_in_capitals():
    from src.store.greek import search_tokens

    assert search_tokens("ΚΑΡΤΕΣ ΣΕ ΠΕΛΑΤΕΣ") == ["καρτεσ", "πελατεσ"]
    assert search_tokens("WHAT IS THE STATUS OF ΑΤΜ CARDS") == ["status", "ατμ", "cards"]


def test_an_empty_name_resolves_to_no_one(conn, monkeypatch):
    """'%%' matched everyone, so a trailing comma in meeting_prep's list built a
    dossier for the most-emailed person."""
    from src import mcp_server
    from src.store.context import resolve_person

    for blank in ("", " ", "  "):
        assert resolve_person(conn, blank) == (None, 0, [])
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.meeting_prep("Καραγιάννης, ")

    assert [a["name"] for a in out["attendees"]] == ["Καραγιάννης"]


def _dated_emails(conn):
    """Emails 11-13 for Καραγιάννης, ids ascending with their dates, so walking
    them in rowid order is not walking them newest first."""
    for i, date in ((11, "2026-01-01"), (12, "2026-02-02"), (13, "2026-03-03")):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (?, ?, ?, ?, 'Κάτι άλλο')",
            (i, i, f"{date}T10:00:00", f"Θέμα {i}"),
        )
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, 3, 'cc')",
            (i,),
        )
    conn.commit()


def test_a_name_query_returns_that_persons_newest_emails(conn):
    from src.store.query import query_by_person

    _dated_emails(conn)

    rows = query_by_person(conn, "Καραγιάννης", limit=3)

    assert [r["email_id"] for r in rows] == [1, 13, 12]


@pytest.mark.parametrize("person", ["Καραγιάννης", "n.karagiannis@example.com"])
def test_combined_query_picks_that_persons_newest_emails(conn, person):
    from src.store.query import query_combined

    _dated_emails(conn)

    rows = query_combined(conn, person=person, limit=3)

    assert [r["email_id"] for r in rows] == [1, 13, 12]


@pytest.mark.parametrize("dense_above", [0, 5000])
@pytest.mark.parametrize("person", ["Καραγιάννης", "n.karagiannis@example.com"])
def test_both_person_plans_find_the_same_emails(conn, monkeypatch, dense_above, person):
    """A person on many emails is found by walking emails newest first, anyone
    else by collecting their emails; both must give the same answer."""
    from src.store import query

    _dated_emails(conn)
    monkeypatch.setattr(query, "DENSE_PERSON_LINKS", dense_above)

    by_person = query.query_by_person(conn, person, limit=3)
    combined = query.query_combined(conn, person=person, limit=3)

    assert [r["email_id"] for r in by_person] == [1, 13, 12]
    assert [r["email_id"] for r in combined] == [1, 13, 12]
    assert {r["person_role"] for r in by_person} == {"to", "cc"}


@pytest.mark.parametrize("name", ["%", "_", ".", "%%"])
def test_a_name_without_a_letter_or_digit_resolves_to_no_one(conn, name):
    """'%' and '_' are LIKE wildcards and '.' ends every initial, so each fitted
    somebody, and the most-emailed of them got the dossier."""
    from src.store.context import resolve_person

    assert resolve_person(conn, name) == (None, 0, [])


def test_the_cli_meeting_prep_skips_empty_names(monkeypatch, tmp_path):
    import types

    from src import cli

    seen = {}

    def fake_prep(conn, people, **kwargs):
        seen["people"] = people
        return {"attendees": []}

    monkeypatch.setattr("src.store.query.meeting_prep", fake_prep)
    monkeypatch.setattr(
        "src.store.schema.get_connection", lambda path: types.SimpleNamespace(close=lambda: None)
    )

    cli.cmd_prep(
        types.SimpleNamespace(
            db=tmp_path / "x.db", people="Καραγιάννης, ", topic=None, days=365, limit=5
        )
    )

    assert seen["people"] == ["Καραγιάννης"]


def test_the_person_plan_switches_above_dense_person_links(conn, monkeypatch):
    """The walk is for people on many emails and the collection for everyone
    else; on the wrong side of the line, either took up to 1.3 s on the replica."""
    import json

    from src.store import query

    _dated_emails(conn)  # Καραγιάννης (id 3) is now on four emails
    ids = json.dumps([3])

    monkeypatch.setattr(query, "DENSE_PERSON_LINKS", 4)
    assert query._linked_to_ids(conn, ids).startswith("e.id IN")
    monkeypatch.setattr(query, "DENSE_PERSON_LINKS", 3)
    assert query._linked_to_ids(conn, ids).startswith("EXISTS")


@pytest.mark.parametrize("person", ["%", "_", " ", "."])
def test_a_person_filter_without_a_letter_or_digit_matches_no_one(conn, person):
    from src.store.query import query_by_person, query_combined

    assert query_by_person(conn, person) == []
    assert query_combined(conn, person=person) == []


def test_a_persons_role_on_an_email_prefers_sender(conn):
    """The lowest role in sort order won, so 'recipient' hid 'sender'."""
    from src.store.query import query_by_person

    conn.executemany(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (2, 3, ?)",
        [("recipient",), ("sender",)],
    )
    conn.commit()

    rows = {r["email_id"]: r["person_role"] for r in query_by_person(conn, "Καραγιάννης")}

    assert rows[2] == "sender"


def test_resolve_person_registers_its_own_functions():
    import sqlite3

    from src.store.context import resolve_person

    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute(
        "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT, department TEXT)"
    )
    raw.execute("CREATE TABLE email_people (email_id INTEGER, person_id INTEGER)")
    raw.execute("INSERT INTO people (id, name) VALUES (1, 'ΚΑΡΑΓΙΑΝΝΗΣ Ν.')")

    row, count, _ = resolve_person(raw, "Καραγιάννης")

    assert (row["name"], count) == ("ΚΑΡΑΓΙΑΝΝΗΣ Ν.", 1)


def _meeting(conn, event_id, subject, start):
    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "is_recurring, is_self_organized, is_cancelled, ingested_at, llm_status) VALUES "
        "(?, ?, ?, ?, ?, 0, 0, 0, '2020-01-01T12:00:00', 'extracted')",
        (event_id, f"ev{event_id}", subject, start, start),
    )


def test_an_attendee_is_matched_by_resolved_person_first(conn):
    """Neither the address nor the name on the invite need match the person row."""
    from src.store.context import get_person_context

    _meeting(conn, 30, "Κοινή σύσκεψη", "2020-02-01T10:00:00")
    conn.execute(
        "INSERT INTO event_attendees (event_id, person_id, email, name, response_status, "
        "is_organizer, is_self) VALUES (30, 3, 'nk@other.example', 'N. K.', 'accepted', 0, 0)"
    )
    conn.commit()

    assert get_person_context(conn, "Καραγιάννης")["last_met"]["subject"] == "Κοινή σύσκεψη"


def test_an_unresolved_attendee_is_matched_by_name_whatever_its_address(conn):
    """An invite from a personal address resolves to no person row; the name is
    what is left, even for someone whose work address is on record."""
    from src.store.context import get_person_context

    _meeting(conn, 31, "Σύσκεψη από προσωπική διεύθυνση", "2020-03-01T10:00:00")
    conn.execute(
        "INSERT INTO event_attendees (event_id, person_id, email, name, response_status, "
        "is_organizer, is_self) VALUES (31, NULL, 'nk@home.example', 'Καραγιάννης Ν.', "
        "'accepted', 0, 0)"
    )
    conn.commit()

    ctx = get_person_context(conn, "Καραγιάννης")

    assert ctx["last_met"]["subject"] == "Σύσκεψη από προσωπική διεύθυνση"


def test_an_attendee_resolved_to_a_namesake_is_not_claimed_by_name(conn):
    """Two people share a full name; a meeting with one is not the other's, though
    the folded name on the invite fits both."""
    from src.store.context import get_person_context

    conn.executemany(
        "INSERT INTO people (id, name, email) VALUES (?, 'ΠΑΠΑΔΟΠΟΥΛΟΣ Γ.', ?)",
        [(4, "g.papadopoulos@example.com"), (5, "gp@elsewhere.example")],
    )
    _meeting(conn, 32, "Σύσκεψη με τον συνονόματο", "2020-04-01T10:00:00")
    conn.execute(
        "INSERT INTO event_attendees (event_id, person_id, email, name, response_status, "
        "is_organizer, is_self) VALUES (32, 5, 'gp@elsewhere.example', "
        "'Παπαδόπουλος Γ.', 'accepted', 0, 0)"
    )
    conn.commit()

    namesake = get_person_context(conn, "gp@elsewhere.example")
    other = get_person_context(conn, "g.papadopoulos@example.com")

    assert namesake["last_met"]["subject"] == "Σύσκεψη με τον συνονόματο"
    assert other["last_met"] is None


def _unresolved_invite(conn, event_id, subject, name):
    _meeting(conn, event_id, subject, "2020-05-01T10:00:00")
    conn.execute(
        "INSERT INTO event_attendees (event_id, person_id, email, name, response_status, "
        "is_organizer, is_self) VALUES (?, NULL, 'x@vendor.example', ?, 'accepted', 0, 0)",
        (event_id, name),
    )
    conn.commit()


def test_a_person_with_a_blank_name_claims_no_unresolved_invite(conn):
    """A blank name made the pattern '%%', which every unresolved attendee fitted:
    a person stored without a display name got every external meeting."""
    from src.store.context import get_person_context

    conn.execute("INSERT INTO people (id, name, email) VALUES (6, '', 'blank@example.com')")
    _unresolved_invite(conn, 33, "Κλήση με προμηθευτή", "Κάποιος Δοκιμαστικός")
    _unresolved_invite(conn, 35, "Κλήση χωρίς όνομα", "")

    assert get_person_context(conn, "blank@example.com")["last_met"] is None


def test_a_one_word_name_claims_no_unresolved_invite(conn):
    """'ΝΙΚΟΣ' fits every Nikos who ever sent an invite."""
    from src.store.context import get_person_context

    conn.execute("INSERT INTO people (id, name, email) VALUES (7, 'ΝΙΚΟΣ', 'nikos@example.com')")
    _unresolved_invite(conn, 34, "Κλήση με άλλον Νίκο", "Νίκος Δοκιμαστικός")

    assert get_person_context(conn, "nikos@example.com")["last_met"] is None


def test_the_teams_cli_says_when_no_thread_held_every_word(conn, tmp_path, capsys, monkeypatch):
    import types

    from src import cli

    _teams(conn)
    monkeypatch.setattr("src.store.schema.get_connection", lambda path: conn)

    cli.cmd_teams_search(
        types.SimpleNamespace(
            db=tmp_path / "x.db", query="cashback ανύπαρκτηλέξη", kind="both", limit=5
        )
    )

    assert "No thread held every word" in capsys.readouterr().out

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


def test_a_name_filter_folds_each_person_once_not_each_email_link():
    """Joined through email_people, sb_fold could run once per link: without
    planner statistics (the store never runs ANALYZE) SQLite scanned the link
    table first, 1.3M calls and 2 s or more for any name. A tiny test store gets
    the other plan, so the shape is what is pinned: people are filtered in a
    subquery, once each."""
    import inspect

    from src.store import query

    shape = "IN (SELECT id FROM people WHERE sb_fold(name) LIKE ?)"
    assert shape in inspect.getsource(query.query_by_person)
    assert shape in inspect.getsource(query.query_combined)


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


def test_each_row_is_scored_once(conn):
    """Selected from a plain subquery, SQLite flattened it and scored every
    matching row a second time for the sort key: a Python call per row."""
    from src.store.greek import _match_score
    from src.store.recall import _search_decisions

    calls = []

    def counting(text, phrase, tokens):
        calls.append(text)
        return _match_score(text, phrase, tokens)

    conn.create_function("sb_match", 3, counting, deterministic=True)
    rows = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    assert _search_decisions(conn, "προυπολογισμος καρτων", 5)
    assert len([c for c in calls if c]) == rows  # the empty one is the registration probe

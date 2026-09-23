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
            (i, i, f"Θέμα {i}", f"Περίληψη για τον προϋπολογισμό {i}"),
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

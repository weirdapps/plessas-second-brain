"""An email can be found by the words of its own subject.

emails_fts indexed the summary and the body, not the subject, and 14% of emails
could not be found by two words of their own subject. v22 adds the folded
subject as a third column, and keyword search tries it first.
"""

import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.query import _sanitize_fts5_query, query_by_keyword
from src.store.schema import (
    create_database,
    get_connection,
    get_schema_version,
    migrate_index_email_subjects,
    run_migrations,
)

_EMAIL = (
    "INSERT INTO emails (message_id, date_received, subject, summary, content) "
    "VALUES (?, '2026-09-01T00:00:00Z', ?, 'a note about something else', 'nothing here')"
)


def _as_v21(path):
    """A store as v21 left it: emails_fts over the summary and the body only."""
    conn = get_connection(str(path))
    for suffix in ("ai", "ad", "au"):
        conn.execute(f"DROP TRIGGER IF EXISTS emails_{suffix}")
    conn.execute("DROP TABLE emails_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE emails_fts USING fts5(summary_f, content_f, "
        "content='emails', content_rowid='id')"
    )
    conn.execute(
        "CREATE TRIGGER emails_ai AFTER INSERT ON emails BEGIN "
        "INSERT INTO emails_fts(rowid, summary_f, content_f) "
        "VALUES (new.id, new.summary_f, new.content_f); END"
    )
    conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
    conn.execute("UPDATE schema_version SET version = 21")
    conn.commit()
    return conn


def _subject_hits(conn, words):
    return conn.execute(
        "SELECT count(*) FROM emails_fts WHERE emails_fts.subject_f MATCH ?",
        (_sanitize_fts5_query(words),),
    ).fetchone()[0]


def test_a_fresh_store_indexes_the_subject(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.commit()

    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION >= 22
    assert _subject_hits(conn, "zebrafinch review") == 1


def test_an_upgraded_store_indexes_the_subjects_it_already_held(tmp_path):
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.commit()

    run_migrations(conn)

    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert _subject_hits(conn, "zebrafinch") == 1
    conn.execute(_EMAIL, (2, "Okapi budget"))
    conn.execute("DELETE FROM emails WHERE message_id = 1")
    conn.commit()
    assert _subject_hits(conn, "okapi") == 1
    assert _subject_hits(conn, "zebrafinch") == 0


def test_the_migration_is_a_no_op_the_second_time(tmp_path):
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.commit()

    first: list[str] = []
    conn.set_trace_callback(first.append)
    run_migrations(conn)
    conn.execute("UPDATE schema_version SET version = 21")
    conn.commit()
    again: list[str] = []
    conn.set_trace_callback(again.append)
    run_migrations(conn)
    conn.set_trace_callback(None)

    assert _subject_hits(conn, "zebrafinch") == 1
    # Immediate: a second unit started with the first waits for it, then finds
    # the work done instead of rebuilding the table the first has just built.
    assert "BEGIN IMMEDIATE" in first
    assert [s for s in again if "DROP TABLE" in s or "'rebuild'" in s] == []


def test_the_subject_is_folded_like_the_rest(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.execute(_EMAIL, (1, "Παρουσίαση πελατών"))
    conn.commit()

    for words in ("παρουσιαση πελατων", "ΠΑΡΟΥΣΙΑΣΗ", "Παρουσίαση"):
        assert _subject_hits(conn, words) == 1, words


def test_keyword_search_still_works_on_a_store_before_v22(tmp_path):
    """A replica can run this code against a database the producer has not
    migrated yet."""
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, content) "
        "VALUES (1, '2026-09-01T00:00:00Z', 'x', 'zebrafinch review', 'y')"
    )
    conn.commit()

    results = query_by_keyword(conn, "zebrafinch", limit=5)

    assert [r["source"] for r in results] == ["summary"]


def test_keyword_search_finds_an_email_by_its_subject_alone(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, content) "
        "VALUES (2, '2026-09-02T00:00:00Z', 'other', 'zebrafinch mentioned', 'x')"
    )
    conn.commit()

    results = query_by_keyword(conn, "zebrafinch review", limit=5)

    assert results[0]["email_id"] == 1
    assert results[0]["source"] == "subject"


def test_a_failed_migration_raises_its_own_error(tmp_path):
    """On a full disk SQLite rolls the transaction back itself, and an explicit
    ROLLBACK then raised 'no transaction is active' in place of the cause."""
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    for n in range(300):
        conn.execute(_EMAIL, (n, " ".join(f"s{n}w{j}" for j in range(60))))
    conn.commit()
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count = {pages + 5}")

    with pytest.raises(sqlite3.OperationalError, match="full"):
        migrate_index_email_subjects(conn)

    assert not conn.in_transaction
    assert [r[1] for r in conn.execute("PRAGMA table_info(emails_fts)")] == [
        "summary_f",
        "content_f",
    ]


def _mail(
    conn,
    n,
    subject,
    summary="a note about something else",
    content="nothing here",
    thread=None,
    mailbox="Inbox",
):
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary, content, "
        "conversation_id, mailbox_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (n, n, f"2026-09-01T00:{n:02d}:00Z", subject, summary, content, thread, mailbox),
    )


def test_one_long_thread_takes_one_slot(tmp_path):
    """Every email of a thread carries its subject: 30 replies filled the page
    and left no room for the emails whose summary was about the words."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 31):
        _mail(conn, n, ("RE: " if n > 1 else "") + "Okapi budget planning", thread="CONV-1")
    for n in range(31, 36):
        _mail(conn, n, "Board", summary="The board approved the okapi budget", thread=f"C{n}")
    conn.commit()

    results = query_by_keyword(conn, "okapi budget", limit=5)

    assert len(results) == 5
    assert sum(r["email_id"] <= 30 for r in results) == 1
    assert sum(r["source"] == "summary" and r["email_id"] > 30 for r in results) == 4


def test_emails_with_no_thread_are_each_their_own(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Okapi budget", thread=None)
    _mail(conn, 2, "Okapi budget", thread="")
    _mail(conn, 3, "Okapi budget", thread=None)
    _mail(conn, 4, "Okapi budget", thread="")
    conn.commit()

    results = query_by_keyword(conn, "okapi budget", limit=5)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3, 4]


def test_summary_matches_come_before_body_matches(tmp_path):
    """The subject hits matched in the summary too and used up the summary
    stage's slots, so summary-only emails lost to body-only ones."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Kiwi plan", summary="kiwi", thread="A")
    _mail(conn, 2, "Kiwi plan two", summary="kiwi", thread="B")
    _mail(conn, 3, "Other", summary="notes on the kiwi crop this year", thread="C")
    _mail(conn, 4, "Another", summary="notes on the kiwi harvest this year", thread="D")
    _mail(conn, 5, "Third", content="kiwi", thread="E")
    _mail(conn, 6, "Fourth", content="kiwi", thread="F")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=4)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3, 4]
    assert "content" not in {r["source"] for r in results}


def test_a_later_source_fills_the_page_past_rows_already_found(tmp_path):
    """Asked only for the slots left, the body search spent them on emails the
    summary search had already returned, and the page came back short."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Alpha", summary="kiwi", thread="A")
    _mail(conn, 2, "Beta", summary="kiwi notes", content="kiwi", thread="B")
    _mail(conn, 3, "Gamma", content="the kiwi was mentioned in passing here", thread="C")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=3)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3]


def _found_by(conn, n, source, text):
    if source == "key_fact":
        conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (n, text))
        return
    conn.execute(
        "INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at) "
        "VALUES (?, ?, ?, 'a.pdf', '/tmp/a.pdf', '2026-09-01')",
        (n, n, n),
    )
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status) "
        "VALUES (?, ?, 'done')",
        (n, text),
    )


@pytest.mark.parametrize("source", ["key_fact", "attachment"])
def test_the_last_sources_fill_the_page_past_rows_already_found(tmp_path, source):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Alpha", summary="kiwi", thread="A")
    _mail(conn, 2, "Beta", summary="kiwi notes", thread="B")
    _found_by(conn, 2, source, "kiwi")
    _mail(conn, 3, "Gamma", thread="C")
    _found_by(conn, 3, source, "the kiwi was mentioned in passing here")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=3)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3]
    assert results[-1]["source"] == source


def test_a_thread_is_shown_by_its_newest_email(tmp_path):
    """The shortest subject ranks best, and in a thread that is the first email;
    the newest is where the thread stands now."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n, subject in enumerate(
        ["Kiwi plan", "RE: Kiwi plan", "RE: RE: Kiwi plan", "FW: RE: RE: Kiwi plan"], start=1
    ):
        _mail(conn, n, subject, thread="T")
    conn.commit()

    results = query_by_keyword(conn, "kiwi plan", limit=5)

    assert [(r["email_id"], r["source"]) for r in results] == [(4, "subject")]


def test_news_articles_of_one_day_are_not_a_thread(tmp_path):
    """News stores a day per pipeline as its conversation_id, a bucket, not a
    thread: same-day articles collapsed to one, and those whose body did not
    repeat the headline were lost."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n, subject in enumerate(
        ["ECB holds rates steady", "ECB chief warns on inflation", "ECB ends bond buying"],
        start=1,
    ):
        _mail(conn, n, subject, thread="news:digest:2026-09-01", mailbox="News")
    conn.commit()

    results = query_by_keyword(conn, "ECB", limit=10)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3]


def test_a_thread_found_by_its_subject_does_not_come_back_for_its_summaries(tmp_path):
    """Its replies' summaries repeat the words too, and took half the page."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 31):
        # The oldest has the best summary: the subject stage shows the newest.
        summary = "kiwi" if n == 1 else f"kiwi rollout update {n}"
        _mail(conn, n, "RE: Kiwi rollout", summary=summary, thread="T")
    for n in range(31, 36):
        _mail(conn, n, "Other", summary=f"notes that mention the kiwi {n}", thread=f"C{n}")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=10)

    assert sum(r["email_id"] <= 30 for r in results) == 1
    assert sorted(r["email_id"] for r in results if r["email_id"] > 30) == [31, 32, 33, 34, 35]


def test_unthreaded_emails_never_share_a_thread_key(tmp_path):
    """The key for an email with no thread was '#<id>', which a real
    conversation_id could equal; and a blank id grouped strangers."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 7, "Okapi budget", thread=None)
    _mail(conn, 8, "Okapi budget", thread="#7")
    _mail(conn, 11, "Okapi budget", thread="  ")
    _mail(conn, 12, "Okapi budget", thread="  ")
    conn.commit()

    results = query_by_keyword(conn, "okapi budget", limit=10)

    assert sorted(r["email_id"] for r in results) == [7, 8, 11, 12]


def test_a_full_page_of_subjects_skips_the_summary_search(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 4):
        _mail(conn, n, f"Okapi {n}", summary="okapi", thread=f"T{n}")
    conn.commit()
    ran: list[str] = []
    conn.set_trace_callback(ran.append)

    results = query_by_keyword(conn, "okapi", limit=2)

    conn.set_trace_callback(None)
    assert len(results) == 2
    assert not [s for s in ran if "summary_f MATCH" in s]


def test_results_carry_no_thread_key(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Okapi", thread="T")
    _mail(conn, 2, "Other", summary="okapi", thread="U")
    _mail(conn, 3, "Third", content="okapi", thread="V")
    conn.commit()

    results = query_by_keyword(conn, "okapi", limit=10)

    assert [r["source"] for r in results] == ["subject", "summary", "content"]
    assert all("thread" not in r for r in results)


def test_a_thread_ranks_by_its_best_match(tmp_path):
    """Shown by its newest email, but ranked by its best: a reply chain's
    prefixes must not sink the thread below a weaker one."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Kiwi", thread="A")
    _mail(conn, 2, "RE: RE: FW: RE: FW: Kiwi", thread="A")
    _mail(conn, 3, "Kiwi plan for the year", thread="B")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=5)

    assert [r["email_id"] for r in results] == [2, 3]


def test_an_email_with_two_matching_facts_comes_back_once(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Alpha", thread="A")
    _found_by(conn, 1, "key_fact", "kiwi")
    _found_by(conn, 1, "key_fact", "kiwi again")
    _mail(conn, 2, "Beta", thread="B")
    _found_by(conn, 2, "key_fact", "the kiwi was mentioned in passing here")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=5)

    assert sorted(r["email_id"] for r in results) == [1, 2]


def test_an_unthreaded_email_ranks_on_its_own_match(tmp_path):
    """Keyed '#<id>', it shared a partition, and so a rank, with a thread whose
    conversation_id happened to be that string."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 7, "Okapi budget review for the whole quarter", thread=None)
    _mail(conn, 8, "Okapi", thread="#7")
    _mail(conn, 9, "Okapi budget", thread="X")
    conn.commit()

    results = query_by_keyword(conn, "okapi", limit=10)

    assert [r["email_id"] for r in results] == [8, 9, 7]


@pytest.mark.parametrize("source", ["content", "key_fact", "attachment"])
def test_a_subject_thread_does_not_come_back_through_a_later_source(tmp_path, source):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row

    def mail(n, subject, text, thread):
        if source == "content":
            _mail(conn, n, subject, content=text, thread=thread)
        else:
            _mail(conn, n, subject, thread=thread)
            _found_by(conn, n, source, text)

    for n in range(1, 31):
        # The oldest matches best: the subject stage shows the newest.
        mail(n, "RE: Kiwi rollout", "kiwi" if n == 1 else f"kiwi rollout update {n}", "T")
    for n in range(31, 36):
        mail(n, "Other", f"notes that mention the kiwi {n}", f"C{n}")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=10)

    assert sum(r["email_id"] <= 30 for r in results) == 1
    assert sorted(r["email_id"] for r in results if r["email_id"] > 30) == [31, 32, 33, 34, 35]


def test_a_recurring_subject_shows_its_newest_threads(tmp_path):
    """Equal scores fell back to conversation_id order, so a weekly report's
    oldest threads could take the page."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 13):
        _mail(conn, n, "Weekly report", thread=f"T{n}")
    conn.commit()

    results = query_by_keyword(conn, "weekly report", limit=3)

    assert [r["email_id"] for r in results] == [12, 11, 10]


@pytest.mark.parametrize("source", ["summary", "content", "key_fact", "attachment"])
def test_one_thread_takes_one_slot_whichever_source_found_it(tmp_path, source):
    """Replies quote each other: a thread whose subject did not match still
    filled the page through its bodies or its summaries."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row

    def mail(n, text, thread):
        if source == "summary":
            _mail(conn, n, "Q3 numbers", summary=text, thread=thread)
        elif source == "content":
            _mail(conn, n, "Q3 numbers", content=text, thread=thread)
        else:
            _mail(conn, n, "Q3 numbers", thread=thread)
            _found_by(conn, n, source, text)

    for n in range(1, 21):
        mail(n, f"the okapi contract is signed {n}", "T")
    for n in range(21, 26):
        mail(n, f"other notes that mention the okapi {n}", f"C{n}")
    conn.commit()

    results = query_by_keyword(conn, "okapi", limit=10)

    assert sum(r["email_id"] <= 20 for r in results) == 1
    assert sorted(r["email_id"] for r in results if r["email_id"] > 20) == [21, 22, 23, 24, 25]


def test_a_thread_one_source_found_is_not_found_again_by_the_next(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Alpha", summary="kiwi", thread="T")
    _mail(conn, 2, "RE: Alpha", content="kiwi", thread="T")
    _mail(conn, 3, "Beta", content="kiwi", thread="U")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=10)

    assert [(r["email_id"], r["source"]) for r in results] == [(1, "summary"), (3, "content")]


def test_a_body_only_search_is_one_row_per_thread_too(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 11):
        _mail(conn, n, "Q3", content=f"okapi {n}", thread="T")
    _mail(conn, 11, "Other", content="okapi elsewhere in a longer body", thread="U")
    conn.commit()

    results = query_by_keyword(conn, "okapi", limit=5, search_content_only=True)

    assert sum(r["email_id"] <= 10 for r in results) == 1
    assert 11 in [r["email_id"] for r in results]


def test_an_unthreaded_email_found_by_two_sources_comes_back_once(tmp_path):
    """Only threads are left out in the query; an email with none can come back
    from the next source, and is skipped there."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Alpha", summary="kiwi", content="kiwi", thread=None)
    _mail(conn, 2, "Beta", content="kiwi in the body of another email", thread=None)
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=5)

    assert sorted(r["email_id"] for r in results) == [1, 2]


def test_blank_subject_emails_are_not_one_thread(tmp_path):
    """With no conversation id and no references, the loader threads an email by
    the hash of its normalized subject: every blank subject shares one hash, and
    strangers collapsed into a single result."""
    from src.store.schema import subject_to_conversation_id

    blank = subject_to_conversation_id("")
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 4):
        _mail(conn, n, "", content=f"the okapi report number {n}", thread=blank)
    conn.commit()

    results = query_by_keyword(conn, "okapi", limit=10)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3]


def test_equal_matches_in_a_thread_show_its_newest(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 4):
        _mail(conn, n, "Status", summary="kiwi", thread="T")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=5)

    assert [(r["email_id"], r["source"]) for r in results] == [(3, "summary")]


def test_equal_attachment_matches_show_the_newest_attachment(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Deck", thread="T")
    for aid, name in ((1, "v1.pdf"), (2, "v2.pdf")):
        conn.execute(
            "INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at) "
            "VALUES (?, 1, 1, ?, '/tmp/x.pdf', '2026-09-01')",
            (aid, name),
        )
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status) "
            "VALUES (?, 'the okapi figures', 'done')",
            (aid,),
        )
    conn.commit()

    results = query_by_keyword(conn, "okapi", limit=5)

    assert [(r["email_id"], r["attachment_filename"]) for r in results] == [(1, "v2.pdf")]


def test_a_padded_thread_id_is_its_own_thread_in_search_and_in_the_thread_view(tmp_path):
    """Search trimmed the id, the thread view did not, so a collapsed thread's
    other email could not be reached."""
    from src.store.query import count_thread, query_thread

    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Okapi", thread="abc")
    _mail(conn, 2, "Okapi", thread="abc ")
    _mail(conn, 3, "Okapi", thread="\t")
    _mail(conn, 4, "Okapi", thread="\t")
    conn.commit()

    results = query_by_keyword(conn, "okapi", limit=10)

    assert sorted(r["email_id"] for r in results) == [1, 2, 4]
    assert [e["email_id"] for e in query_thread(conn, 2)] == [2]
    assert [e["email_id"] for e in query_thread(conn, 4)] == [3, 4]
    assert count_thread(conn, 3) == 2


@pytest.mark.parametrize("source", ["subject", "summary", "content"])
def test_a_threads_row_says_how_many_of_its_emails_matched(tmp_path, source):
    """One row per thread hides the rest of it. Half the corpus is threaded by
    subject alone (43,399 emails, from before Outlook's conversation ids), and a
    recurring report's matching emails read as one row. thread_matches says how
    many matched, so a caller knows to open email_thread."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row

    def mail(n, thread):
        if source == "subject":
            _mail(conn, n, "Okapi weekly report", thread=thread)
        elif source == "summary":
            _mail(conn, n, "Report", summary="the okapi weekly report", thread=thread)
        else:
            _mail(conn, n, "Report", content="the okapi weekly report", thread=thread)

    for n in range(1, 5):
        mail(n, "T")
    mail(5, "C5")
    mail(6, None)
    conn.commit()

    results = {r["email_id"]: r for r in query_by_keyword(conn, "okapi weekly", limit=10)}

    assert sorted(results) == [4, 5, 6]
    assert results[4]["thread_matches"] == 4
    assert "thread_matches" not in results[5]  # alone in its thread
    assert "thread_matches" not in results[6]  # no thread at all

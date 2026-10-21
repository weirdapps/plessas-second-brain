"""Schema v41: received times in UTC, and three repairs of mail and calendar rows.

The retired Apple Mail exporter stored a Mac's Athens wall time with no zone, and standalone
documents their file's mtime the same way, beside the export's UTC with a Z. Every reader takes
a time with no zone for UTC, so each such row was two or three hours late. v41 converts them,
and recomputes the one stored value read from them, each email thread's newest time.
"""

import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store import schema
from src.store.schema import (
    create_database,
    get_schema_version,
    migrate_mail_hygiene,
    migrate_received_times_utc,
    received_time_utc,
    run_migrations,
    set_schema_version,
)
from tests import factories


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _date(conn, email_id):
    return conn.execute("SELECT date_received FROM emails WHERE id = ?", (email_id,)).fetchone()[0]


# --- the conversion of one value ---


@pytest.mark.parametrize(
    ("stored", "utc"),
    [
        # Summer time, +03:00.
        ("2026-04-08T08:39:28", "2026-04-08T05:39:28Z"),
        # Winter time, +02:00.
        ("2026-01-15T10:00:00", "2026-01-15T08:00:00Z"),
        # Past midnight in Athens is the day before in UTC.
        ("2026-05-02T01:30:00", "2026-05-01T22:30:00Z"),
        # The last hour before the clocks go forward (2026-03-29 03:00), and the first after.
        ("2026-03-29T02:59:59", "2026-03-29T00:59:59Z"),
        ("2026-03-29T04:00:00", "2026-03-29T01:00:00Z"),
        # A document's mtime keeps its microseconds.
        ("2024-07-15T17:11:50.754618", "2024-07-15T14:11:50.754618Z"),
    ],
)
def test_a_naive_time_is_read_as_athens_wall_time(stored, utc):
    assert received_time_utc(stored) == utc


def test_the_hour_the_clocks_go_back_is_read_as_its_first_pass():
    # 2025-10-26 03:30 happened twice in Athens: 00:30Z in summer time, then 01:30Z.
    assert received_time_utc("2025-10-26T03:30:00") == "2025-10-26T00:30:00Z"


def test_an_hour_the_clocks_skip_is_read_with_the_offset_before_the_change():
    assert received_time_utc("2026-03-29T03:30:00") == "2026-03-29T01:30:00Z"


@pytest.mark.parametrize(
    "value",
    [
        "2026-04-08T05:39:28Z",
        "2026-06-20T05:31:00+00:00",
        "2026-10-16T09:16:09.841Z",
        "2026-04-08 08:39:28",
        "2026-02-30T10:00:00",
        "",
        None,
    ],
)
def test_any_other_value_is_left_alone(value):
    assert received_time_utc(value) is None


# --- the migration ---


def test_v41_converts_naive_mail_automation_and_documents_only(conn):
    mail = factories.email(conn, date_received="2026-04-08T08:39:28", source_class="mail")
    automation = factories.email(
        conn, date_received="2026-01-15T10:00:00", source_class="automation"
    )
    document = factories.email(
        conn,
        date_received="2024-07-15T17:11:50.754618",
        source_class="document",
        mailbox_name="External",
    )
    already = factories.email(conn, date_received="2026-07-01T09:00:00Z", source_class="mail")
    news = factories.email(
        conn, date_received="2026-07-01T09:00:00+00:00", source_class="news", mailbox_name="News"
    )
    note = factories.email(
        conn,
        date_received="2026-10-16T14:48:08.076493",
        source_class="session_note",
        mailbox_name="External",
    )

    migrate_received_times_utc(conn)

    assert _date(conn, mail) == "2026-04-08T05:39:28Z"
    assert _date(conn, automation) == "2026-01-15T08:00:00Z"
    assert _date(conn, document) == "2024-07-15T14:11:50.754618Z"
    assert _date(conn, already) == "2026-07-01T09:00:00Z"
    assert _date(conn, news) == "2026-07-01T09:00:00+00:00"
    # Session notes are dated by their own repair.
    assert _date(conn, note) == "2026-10-16T14:48:08.076493"


def test_a_second_run_changes_nothing(conn, monkeypatch):
    monkeypatch.setattr(schema, "_RECEIVED_BATCH", 2)
    ids = [
        factories.email(conn, date_received=f"2026-04-0{day}T08:00:00", subject=f"s{day}")
        for day in range(1, 6)
    ]
    migrate_received_times_utc(conn)
    first = [_date(conn, i) for i in ids]
    migrate_received_times_utc(conn)

    assert [_date(conn, i) for i in ids] == first
    assert first == [f"2026-04-0{day}T05:00:00Z" for day in range(1, 6)]


def test_v41_runs_from_v40_and_stamps_the_version(conn):
    email_id = factories.email(conn, date_received="2026-04-08T08:39:28")
    set_schema_version(conn, 40)

    run_migrations(conn)

    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION == 41
    assert _date(conn, email_id) == "2026-04-08T05:39:28Z"


class _Racing(sqlite3.Connection):
    """A connection that lets another job write the store between reading the naive rows and
    taking the write lock, as a second job starting at the same moment would."""

    meanwhile = None

    def execute(self, sql, *args):
        if _Racing.meanwhile is not None and sql == "BEGIN IMMEDIATE":
            meanwhile, _Racing.meanwhile = _Racing.meanwhile, None
            meanwhile()
        return super().execute(sql, *args)


def test_two_jobs_converting_at_once_convert_each_row_once(tmp_path):
    """The other job converts every row, then a row is set from Outlook (maint mail-times):
    this job's writes, computed from what it read before, find the value gone and leave it."""
    path = str(tmp_path / "brain.db")
    setup = create_database(path)
    ids = [
        factories.email(setup, date_received=f"2026-04-0{day}T08:00:00", subject=f"s{day}")
        for day in range(1, 4)
    ]
    setup.close()

    def other_job():
        other = sqlite3.connect(path)
        migrate_received_times_utc(other)
        other.execute(
            "UPDATE emails SET date_received = '2026-04-01T06:00:00Z' WHERE id = ?", (ids[0],)
        )
        other.commit()
        other.close()

    racing = sqlite3.connect(path, factory=_Racing)
    _Racing.meanwhile = other_job
    migrate_received_times_utc(racing)
    dates = [
        racing.execute("SELECT date_received FROM emails WHERE id = ?", (i,)).fetchone()[0]
        for i in ids
    ]
    racing.close()

    assert _Racing.meanwhile is None  # the other job ran
    assert dates == ["2026-04-01T06:00:00Z", "2026-04-02T05:00:00Z", "2026-04-03T05:00:00Z"]


def test_each_email_threads_newest_time_is_recomputed(conn):
    """Thread.last_at compared both forms as text: '...T10:15:26' outranked
    '...T08:00:00Z', though it is 07:15 UTC. A thread whose items did not change is not
    loaded again, so the stored value stays unless v41 writes it."""
    naive = factories.email(
        conn, date_received="2026-06-09T10:15:26", subject="plan", conversation_id="conv-a"
    )
    utc = factories.email(
        conn, date_received="2026-06-09T08:00:00Z", subject="Re: plan", conversation_id="conv-a"
    )
    alone = factories.email(conn, date_received="2026-05-02T01:30:00", conversation_id="")
    teams = "teams:7"
    for key, last_at in (
        ("email:conv-a", "2026-06-09T10:15:26"),
        (f"email1:{alone}", "2026-05-02T01:30:00"),
        (teams, "2026-06-01T00:00:00Z"),
    ):
        conn.execute(
            "INSERT INTO consolidation_threads (thread_key, signature, last_at) VALUES (?, 'sig', ?)",
            (key, last_at),
        )
    conn.commit()

    migrate_received_times_utc(conn)

    last = dict(conn.execute("SELECT thread_key, last_at FROM consolidation_threads"))
    assert _date(conn, naive) == "2026-06-09T07:15:26Z"
    assert last["email:conv-a"] == _date(conn, utc) == "2026-06-09T08:00:00Z"
    assert last[f"email1:{alone}"] == "2026-05-01T22:30:00Z"
    assert last[teams] == "2026-06-01T00:00:00Z"


def test_the_old_spelling_of_sent_items_is_the_exports(conn):
    old = factories.email(conn, mailbox_name="SentItems")
    inbox = factories.email(conn, mailbox_name="Inbox", subject="other")

    migrate_mail_hygiene(conn)

    names = dict(conn.execute("SELECT id, mailbox_name FROM emails"))
    assert names == {old: "Sent Items", inbox: "Inbox"}


def test_a_mail_with_no_sender_row_is_linked_to_its_sender(conn):
    known = factories.person(conn, "Notices", "notices@example.com")
    lost = factories.email(conn, sender=("Notices", "notices@example.com"), subject="one")
    conn.execute(
        "DELETE FROM email_people WHERE email_id = ? AND role_in_email = 'sender'", (lost,)
    )
    unnamed = factories.email(conn, sender=None, subject="two")
    conn.execute(
        "UPDATE emails SET sender_name = '', sender_address = 'system@example.com' WHERE id = ?",
        (unnamed,),
    )
    conn.commit()

    migrate_mail_hygiene(conn)
    migrate_mail_hygiene(conn)  # a second run adds nothing

    senders = conn.execute(
        "SELECT ep.email_id, p.id, p.name, p.email FROM email_people ep"
        " JOIN people p ON p.id = ep.person_id WHERE ep.role_in_email = 'sender'"
        " AND ep.email_id IN (?, ?) ORDER BY ep.email_id",
        (lost, unnamed),
    ).fetchall()
    assert [tuple(r) for r in senders] == [
        (lost, known, "Notices", "notices@example.com"),
        (unnamed, senders[1][1], "system@example.com", "system@example.com"),
    ]


def test_a_failed_calendar_extraction_is_offered_again(conn):
    failed = factories.calendar_event(conn, llm_status="failed")
    done = factories.calendar_event(conn, llm_status="extracted")

    migrate_mail_hygiene(conn)

    status = dict(conn.execute("SELECT id, llm_status FROM calendar_events"))
    assert status == {failed: "pending", done: "extracted"}

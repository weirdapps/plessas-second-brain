"""Keyword search with recency fused and editions collapsed: search_attachments and
search_emails' keyword mode (query_by_keyword), on synthetic monthly editions."""

import pytest

from src.store.query import query_by_keyword, search_attachments
from src.store.schema import create_database
from tests import factories
from tests.patching import unset_env

QUERY = "okapi conversion ratio"
MONTHS = ("31.03.2026", "30.04.2026", "31.05.2026", "30.06.2026", "31.07.2026", "31.08.2026")


@pytest.fixture(autouse=True)
def _fused(monkeypatch):
    unset_env(monkeypatch, "BRAIN_QUERY_RANKING")


def _monthly_store(tmp_path):
    """Twelve older attachments that match the words better, then six monthly editions of
    one workbook, each mailed in the month after it covers. Returns (conn, edition ids
    oldest first)."""
    conn = create_database(str(tmp_path / "brain.db"))
    for n in range(12):
        factories.attachment(
            conn,
            email_id=factories.email(
                conn, subject=f"Press clipping {n}", date_received=f"2026-01-{n + 10}T09:00:00Z"
            ),
            filename=f"clipping-{n}.pdf",
            text="okapi conversion ratio " * 5 + f"clipping {n}",
        )
    editions = []
    for month, day in enumerate(MONTHS):
        sent = f"2026-{month + 4:02d}-22T09:00:00Z"
        email_id = factories.email(conn, subject=f"Okapi workbook {day}", date_received=sent)
        editions.append(
            factories.attachment(
                conn,
                email_id=email_id,
                filename=f"Okapi Workbook {day}.xlsx",
                text="Monthly sheet. " * 40 + "The okapi conversion ratio by product.",
            )
        )
    return conn, editions


def test_the_newest_edition_ranks_behind_the_older_ones_by_relevance_alone(tmp_path, monkeypatch):
    """The audit's finding, reproduced: what this change is measured against."""
    monkeypatch.setenv("BRAIN_QUERY_RANKING", "relevance")
    conn, editions = _monthly_store(tmp_path)

    rows = search_attachments(conn, QUERY, limit=20)

    ids = [r["attachment_id"] for r in rows]
    assert ids.index(editions[-1]) >= 12
    assert all("older_editions" not in r for r in rows)


def test_search_attachments_shows_the_newest_edition_in_its_first_five(tmp_path):
    conn, editions = _monthly_store(tmp_path)

    rows = search_attachments(conn, QUERY, limit=5)

    ids = [r["attachment_id"] for r in rows]
    assert editions[-1] in ids
    newest = rows[ids.index(editions[-1])]
    assert newest["older_editions"] == 5
    assert newest["editions"] == editions[-2::-1]  # the others, newest first
    assert not set(editions[:-1]) & set(ids)  # one row for the series
    assert newest["snippet"] and newest["ref"] == editions[-1]  # a whole row, enveloped


def test_search_attachments_shows_the_edition_a_query_dates(tmp_path):
    conn, editions = _monthly_store(tmp_path)

    # Only the filename holds "workbook" and the date, so every row is a partial match.
    rows = search_attachments(conn, "okapi workbook 31.05.2026", limit=20)

    series = [r for r in rows if r["attachment_id"] in editions]
    assert [r["attachment_id"] for r in series] == [editions[2]]
    assert series[0]["older_editions"] == 5 and series[0]["partial_match"]


def test_search_attachments_collapses_copies_of_one_file(tmp_path):
    """The same file mailed twice under two names is one row, the newer copy."""
    conn = create_database(str(tmp_path / "brain.db"))
    first, second = (
        factories.attachment(
            conn,
            email_id=factories.email(conn, subject=subject, date_received=sent),
            filename=filename,
            text="The okapi agreement, signed.",
            sha256="0" * 64,
        )
        for subject, sent, filename in (
            ("Agreement", "2026-03-01T09:00:00Z", "agreement.pdf"),
            ("Fwd: Agreement", "2026-03-04T09:00:00Z", "okapi agreement signed.pdf"),
        )
    )

    rows = search_attachments(conn, "okapi agreement", limit=5)

    assert [r["attachment_id"] for r in rows] == [second]
    assert rows[0]["editions"] == [first]


def test_search_attachments_still_returns_limit_rows(tmp_path):
    conn, _editions = _monthly_store(tmp_path)

    assert len(search_attachments(conn, QUERY, limit=5)) == 5
    # Twelve clippings and one series: thirteen rows in all.
    assert len(search_attachments(conn, QUERY, limit=50)) == 13


def _mail_store(tmp_path):
    """Six monthly report emails with dated subjects, and an old email whose subject holds
    the words, plus newer mail that only mentions them in the body."""
    conn = create_database(str(tmp_path / "brain.db"))
    old = factories.email(
        conn,
        subject="Okapi conversion ratio review",
        date_received="2025-01-10T09:00:00Z",
        summary="The review.",
        content="Nothing more.",
    )
    reports = [
        factories.email(
            conn,
            subject=f"Okapi conversion ratio {day}",
            date_received=f"2026-{month + 4:02d}-22T09:00:00Z",
            summary="Monthly figures.",
            content="Figures attached.",
        )
        for month, day in enumerate(MONTHS)
    ]
    mentions = [
        factories.email(
            conn,
            subject=f"Weekly notes {n}",
            date_received=f"2026-09-{n + 10}T09:00:00Z",
            summary="Notes.",
            content="We discussed the okapi conversion ratio again.",
        )
        for n in range(3)
    ]
    return conn, old, reports, mentions


def test_keyword_search_collapses_dated_subjects_to_the_newest(tmp_path):
    conn, old, reports, _mentions = _mail_store(tmp_path)

    rows = query_by_keyword(conn, QUERY, limit=10)

    ids = [r["email_id"] for r in rows]
    assert reports[-1] in ids and not set(reports[:-1]) & set(ids)
    newest = rows[ids.index(reports[-1])]
    assert newest["older_editions"] == 5
    assert newest["editions"] == reports[-2::-1]


def test_keyword_search_keeps_subject_matches_ahead_of_newer_body_matches(tmp_path):
    conn, old, reports, mentions = _mail_store(tmp_path)

    rows = query_by_keyword(conn, QUERY, limit=10)

    ids = [r["email_id"] for r in rows]
    assert {old, reports[-1]} == set(ids[:2])  # both subject matches first
    assert set(ids[2:]) == set(mentions)


def test_keyword_search_by_relevance_alone_is_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_QUERY_RANKING", "relevance")
    conn, old, reports, mentions = _mail_store(tmp_path)

    rows = query_by_keyword(conn, QUERY, limit=10)

    assert len(rows) == 10
    assert all("older_editions" not in r for r in rows)


def test_thread_matches_is_still_counted_for_the_row_shown(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    first = factories.email(conn, subject="Okapi plan", date_received="2026-09-01T09:00:00Z")
    factories.email(conn, subject="RE: Okapi plan", date_received="2026-09-02T09:00:00Z")

    rows = query_by_keyword(conn, "okapi plan", limit=5)

    assert len(rows) == 1 and rows[0]["thread_matches"] == 2
    assert rows[0]["email_id"] != first  # the thread's newest, as before

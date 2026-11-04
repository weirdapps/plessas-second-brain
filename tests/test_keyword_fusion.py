"""Keyword ranking across stages (issue 178): document shells collapsed, documents
carrying their attachment's summary, attachment hits deduplicated by content hash, and
the stage waterfall replaced by reciprocal rank fusion with a recency prior.

Every test runs on a store with the gated steps applied and on one without them."""

import pytest

from src.store.query import query_by_keyword
from src.store.schema import create_database
from tests import factories
from tests.patching import unset_env

QUERY = "okapi conversion ratio"


@pytest.fixture(autouse=True)
def _fused(monkeypatch):
    unset_env(monkeypatch, "BRAIN_QUERY_RANKING")


@pytest.fixture(params=[True, False], ids=["gated", "ungated"])
def conn(request, tmp_path):
    return create_database(str(tmp_path / "brain.db"), gated=request.param)


def _document(conn, subject, *, text, summary=None, date="2026-09-01T09:00:00Z", **columns):
    """A standalone document as ingest_document stores it: an unthreaded anchor row with no
    summary of its own, and the file as its attachment. Returns (email id, attachment id)."""
    email_id = factories.email(
        conn,
        subject=subject,
        sender=("External Document", None),
        to=(),
        mailbox_name="External",
        source_class=columns.pop("source_class", "document"),
        conversation_id=None,
        summary=None,
        content=None,
        date_received=date,
    )
    attachment_id = factories.attachment(
        conn, email_id=email_id, text=text, summary=summary, **columns
    )
    return email_id, attachment_id


def test_documents_sharing_a_subject_are_one_row(conn):
    shells = [
        _document(
            conn,
            "[Document] Research",
            filename=f"note-{n}.pdf",
            text=f"okapi conversion ratio, part {n}. " * (5 - n),
            date=f"2026-09-0{n + 1}T09:00:00Z",
        )[0]
        for n in range(5)
    ]
    mail = [
        factories.email(
            conn,
            subject=f"Weekly notes {n}",
            date_received=f"2026-08-1{n}T09:00:00Z",
            content="We discussed the okapi conversion ratio.",
        )
        for n in range(3)
    ]

    rows = query_by_keyword(conn, QUERY, limit=5)

    ids = [r["email_id"] for r in rows]
    folder = [r for r in rows if r["email_id"] in shells]
    assert len(folder) == 1
    assert folder[0]["older_editions"] == 4
    assert set(folder[0]["editions"]) == set(shells) - {folder[0]["email_id"]}
    assert folder[0]["email_id"] == shells[0]  # the best match stands for the folder
    assert set(mail) <= set(ids)  # the mail is no longer crowded off the page


def test_a_shell_collapses_only_with_its_own_source_class(conn):
    document = _document(conn, "[Document] Research", text="okapi conversion ratio")[0]
    note = _document(
        conn, "[Document] Research", text="okapi conversion ratio", source_class="session_note"
    )[0]

    ids = [r["email_id"] for r in query_by_keyword(conn, QUERY, limit=5)]

    assert {document, note} <= set(ids)


def test_a_document_row_carries_its_attachment_summary(conn):
    document = _document(
        conn,
        "[Document] Research",
        text="okapi conversion ratio by product",
        summary="A synthetic summary of the okapi note.",
    )[0]
    mail = factories.email(
        conn, subject="Okapi conversion ratio", summary="The mail's own summary."
    )

    rows = {r["email_id"]: r for r in query_by_keyword(conn, QUERY, limit=5)}

    assert rows[document]["summary"] == "A synthetic summary of the okapi note."
    assert rows[mail]["summary"] == "The mail's own summary."


def test_one_file_mailed_to_several_threads_is_one_row(conn):
    digest = "ab" * 32
    copies = [
        factories.attachment(
            conn,
            email_id=factories.email(
                conn,
                subject=f"Forwarded {n}",
                date_received=f"2026-09-0{n + 1}T09:00:00Z",
                content="See attached.",
            ),
            filename="ratios.pdf",
            text="okapi conversion ratio by product",
            sha256=digest,
        )
        for n in range(3)
    ]
    other = factories.attachment(
        conn,
        email_id=factories.email(conn, subject="Another file", content="See attached."),
        filename="other.pdf",
        text="okapi conversion ratio for the branches",
        sha256="cd" * 32,
    )

    rows = query_by_keyword(conn, QUERY, limit=5)

    emails = {
        conn.execute("SELECT email_id FROM attachments WHERE id = ?", (a,)).fetchone()[0]
        for a in copies
    }
    shown = [r for r in rows if r["email_id"] in emails]
    assert len(shown) == 1 and shown[0]["older_editions"] == 2
    other_email = conn.execute("SELECT email_id FROM attachments WHERE id = ?", (other,)).fetchone()
    assert other_email[0] in [r["email_id"] for r in rows]


def test_an_email_matching_in_several_stages_is_not_buried_under_subject_matches(conn):
    """The strict waterfall placed every subject match before any summary or body match:
    twelve old subject matches filled a five-row page."""
    for n in range(12):
        factories.email(
            conn,
            subject=f"Okapi conversion ratio item {n}",
            date_received=f"2024-01-{n + 10}T09:00:00Z",
            summary="Old.",
            content="Old.",
        )
    strong = factories.email(
        conn,
        subject="Branch review",
        date_received="2026-09-20T09:00:00Z",
        summary="The okapi conversion ratio fell in every branch.",
        content="The okapi conversion ratio fell; the okapi conversion ratio is the KPI.",
    )

    ids = [r["email_id"] for r in query_by_keyword(conn, QUERY, limit=5)]

    assert strong in ids


def test_the_row_says_the_first_stage_that_matched_it(conn):
    email_id = factories.email(
        conn,
        subject="Okapi conversion ratio",
        summary="The okapi conversion ratio.",
        content="The okapi conversion ratio.",
    )

    (row,) = query_by_keyword(conn, QUERY, limit=5)

    assert row["email_id"] == email_id
    assert row["source"] == "subject" and row["matched_in"] == "subject"


def test_recency_breaks_an_even_match(conn):
    old = factories.email(
        conn,
        subject="Notes A",
        date_received="2025-01-10T09:00:00Z",
        content="okapi conversion ratio",
    )
    new = factories.email(
        conn,
        subject="Notes B",
        date_received="2026-09-10T09:00:00Z",
        content="okapi conversion ratio",
    )

    ids = [r["email_id"] for r in query_by_keyword(conn, QUERY, limit=5)]

    assert ids == [new, old]


def test_relevance_alone_keeps_the_waterfall(conn, monkeypatch):
    monkeypatch.setenv("BRAIN_QUERY_RANKING", "relevance")
    shells = [
        _document(conn, "[Document] Research", filename=f"n{n}.pdf", text="okapi conversion ratio")[
            0
        ]
        for n in range(3)
    ]

    rows = query_by_keyword(conn, QUERY, limit=5)

    assert {r["email_id"] for r in rows} == set(shells)
    assert all("older_editions" not in r for r in rows)


def test_recall_joins_a_shell_sibling_semantic_search_found_to_the_shell_row(conn):
    from src.store.recall import recall

    matched = _document(
        conn, "[Document] Research", filename="a.pdf", text="okapi conversion ratio"
    )[0]
    sibling = _document(
        conn,
        "[Document] Research",
        filename="b.pdf",
        text="unrelated words",
        summary="A synthetic summary of the sibling.",
    )[0]
    alone = _document(
        conn,
        "[Document] Elsewhere",
        filename="c.pdf",
        text="unrelated words",
        summary="A synthetic summary of another note.",
    )[0]

    res = recall(conn, QUERY, semantic_candidates=lambda c, q, n: [sibling, alone])

    rows = {r["email_id"]: r for r in res["emails"]}
    assert sibling not in rows and sibling in rows[matched]["editions"]
    assert rows[alone]["summary"] == "A synthetic summary of another note."


def test_a_probe_counts_rows_by_the_waterfall_without_snippets(conn):
    """The excluded counts need each match's class only: a probe takes the waterfall,
    which stops once its page is full, and reads no attachment snippet."""
    shells = [
        _document(conn, "[Document] Research", filename=f"n{n}.pdf", text="okapi conversion ratio")[
            0
        ]
        for n in range(3)
    ]
    ran: list[str] = []
    conn.set_trace_callback(ran.append)

    rows = query_by_keyword(conn, QUERY, limit=50, probe=True)

    assert sorted(r["email_id"] for r in rows) == shells
    assert {r["source_class"] for r in rows} == {"document"}
    assert not [sql for sql in ran if "snippet(attachment_content_fts" in sql]

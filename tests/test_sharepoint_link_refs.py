"""What a SharePoint link was found in, the date its document takes, and the twins it must not make.

sharepoint_links.message_id names the item a link came from. It was always an email's message
id; a link in Teams is recorded as "teams:<teams_messages.id>", and one listed by the
shared-with-me backfill as "shared-with-me". The document a fetch makes is dated by that item:
the email's arrival, the Teams message's composition, or the date the backfill line gives.

A fetched file whose bytes a mail attachment already holds as text makes no second document
(the 699 twin documents forgotten on 2026-10-07 were exactly this): its link points at the
email that holds the text.
"""

import hashlib
from pathlib import Path

import pytest

from src.export import sharepoint_fetcher
from src.export.sharepoint_fetcher import (
    SHARED_WITH_ME,
    SharepointFetchResult,
    link_source,
    record_link_in_db,
    teams_ref,
)
from src.extract.sharepoint_ingest import fetch_and_ingest, ingest_fetched_backlog, link_date
from src.store.schema import create_database

URL = "https://contoso.sharepoint.com/sites/Team/Shared%20Documents/Plan.txt"
WORDS = "The plan for next year, in enough words to pass the noise filter easily."


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    c.execute(
        "INSERT INTO emails (message_id, date_received, subject)"
        " VALUES (101, '2026-03-01T09:00:00Z', 'see the plan')"
    )
    chat = c.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at)"
        " VALUES ('19:example@thread.v2', 'group', '2026-09-01')"
    ).lastrowid
    c.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at, is_system)"
        " VALUES (?, ?, '2026-10-02T08:30:00.0000000Z', 0)",
        (f"{chat}::1760000000000", chat),
    )
    c.commit()
    yield c
    c.close()


def _teams_id(conn) -> int:
    return conn.execute("SELECT id FROM teams_messages").fetchone()[0]


def _fake_fetch(monkeypatch, body=WORDS, name="Plan.txt"):
    def fake(url, out_dir, managed_host=None):
        f = Path(out_dir) / name
        f.write_text(body)
        return SharepointFetchResult(
            url=url, status="ok", local_path=f, file_name=f.name, file_size=f.stat().st_size
        )

    monkeypatch.setattr(sharepoint_fetcher, "fetch_sharepoint_link", fake)


def _date_of(conn, message_id):
    return conn.execute(
        "SELECT date_received FROM emails WHERE message_id = ?", (message_id,)
    ).fetchone()[0]


def _sharepoint_documents(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE file_path LIKE 'text:sharepoint:%'"
    ).fetchone()[0]


# --- What a link was found in ---------------------------------------------------------------


def test_a_ref_says_where_its_link_was_found():
    assert teams_ref(42) == "teams:42"
    assert link_source(teams_ref(42)) == "teams"
    assert link_source(SHARED_WITH_ME) == "shared-with-me"
    assert link_source(101) == "email"
    assert link_source("AAMk-1") == "email"


def test_a_link_is_dated_by_the_item_it_was_found_in(conn):
    assert link_date(conn, 101) == "2026-03-01T09:00:00Z"
    assert link_date(conn, "101") == "2026-03-01T09:00:00Z", "the column stores refs as text"
    assert link_date(conn, teams_ref(_teams_id(conn))) == "2026-10-02T08:30:00.0000000Z"
    assert link_date(conn, SHARED_WITH_ME) is None, "the backfill passes its date in"
    assert link_date(conn, teams_ref(999)) is None
    assert link_date(conn, "teams:not-a-number") is None
    assert link_date(conn, None) is None


def test_a_file_linked_in_teams_is_dated_by_the_message(conn, monkeypatch):
    _fake_fetch(monkeypatch)

    result, document = fetch_and_ingest(conn, URL, teams_ref(_teams_id(conn)))

    assert result.status == "ok"
    assert _date_of(conn, document) == "2026-10-02T08:30:00.0000000Z"


def test_a_date_passed_in_dates_the_document(conn, monkeypatch):
    _fake_fetch(monkeypatch)

    _, document = fetch_and_ingest(conn, URL, SHARED_WITH_ME, date="2026-05-04T10:00:00Z")

    assert _date_of(conn, document) == "2026-05-04T10:00:00Z"


def test_a_page_linked_in_teams_is_dated_by_the_message(conn, monkeypatch):
    page = "https://contoso.sharepoint.com/sites/News/SitePages/Launch.aspx"
    monkeypatch.setattr(
        sharepoint_fetcher,
        "fetch_sharepoint_page",
        lambda url: sharepoint_fetcher.SharepointPageResult(
            url=url, status="ok", title="Launch", html="<p>The launch plan, in words.</p>"
        ),
    )

    _, document = fetch_and_ingest(conn, page, teams_ref(_teams_id(conn)))

    assert _date_of(conn, document) == "2026-10-02T08:30:00.0000000Z"


# --- No twin documents ----------------------------------------------------------------------


def _mail_attachment(conn, body: str, *, text: str, status: str = "extracted") -> None:
    """A mail attachment of email 101 holding `body`, read by Phase 1 into `text`."""
    email_id = conn.execute("SELECT id FROM emails WHERE message_id = 101").fetchone()[0]
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_size, file_path,"
        " exported_at, sha256) VALUES (?, 101, 'plan.txt', ?, '/mail/plan.txt', 'now', ?)",
        (email_id, len(body), hashlib.sha256(body.encode()).hexdigest()),
    ).lastrowid
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status) VALUES (?, ?, 'text', ?)",
        (att, text, status),
    )
    conn.commit()


def test_bytes_a_mail_attachment_holds_as_text_make_no_second_document(conn, monkeypatch):
    _mail_attachment(conn, WORDS, text=WORDS)
    _fake_fetch(monkeypatch)

    result, document = fetch_and_ingest(conn, URL, 101)
    record_link_in_db(
        conn, url=URL, message_id="101", status=result.status, document_message_id=document
    )

    assert (result.status, document) == ("ok", 101)
    assert _sharepoint_documents(conn) == 0
    row = conn.execute("SELECT last_status, document_message_id FROM sharepoint_links").fetchone()
    assert tuple(row) == ("ok", 101), "the link points at the email that holds the text"


@pytest.mark.parametrize(
    ("text", "status"),
    [("", "extracted"), (WORDS, "failed"), (WORDS, "encrypted")],
    ids=["no text", "failed", "encrypted"],
)
def test_a_twin_that_holds_no_text_does_not_stop_the_document(conn, monkeypatch, text, status):
    _mail_attachment(conn, WORDS, text=text, status=status)
    _fake_fetch(monkeypatch)

    _, document = fetch_and_ingest(conn, URL, 101)

    assert document not in (None, 101)
    assert _sharepoint_documents(conn) == 1


def test_the_backlog_links_a_held_twin_instead_of_storing_it(conn, tmp_path):
    _mail_attachment(conn, WORDS, text=WORDS)
    root = tmp_path / "sharepoint"
    root.mkdir()
    kept = root / "Plan.txt"
    kept.write_text(WORDS)
    record_link_in_db(
        conn,
        url=URL,
        message_id="101",
        status="ok",
        fetched_path=str(kept),
        file_name="Plan.txt",
        file_size=kept.stat().st_size,
    )

    stats = ingest_fetched_backlog(conn, [root])

    assert (stats["ingested"], stats["already"], stats["linked"]) == (0, 1, 1)
    assert _sharepoint_documents(conn) == 0
    doc = conn.execute("SELECT document_message_id FROM sharepoint_links").fetchone()[0]
    assert doc == 101

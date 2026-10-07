"""The repair of what the old link scanner and the old page fetch left in sharepoint_links.

Mangled links: the scan once kept HTML entities in URLs and cut the Safe Links copy of a URL
at its apostrophe, so the table holds links no email contains. They 404 until the attempt cap
retires them, or read as no content and settle at once. Unread pages: page links recorded
'ok' whose document is not the page's text. Neither repair fetches anything; the retry pass
reads what they queue.
"""

from argparse import Namespace

import pytest

from src.export.sharepoint_fetcher import record_link_in_db, retry_candidates
from src.export.sharepoint_repair import refused_links, reread_pages, rescan_links
from src.store.email_html import save_html
from src.store.schema import create_database

SITE = "https://contoso.sharepoint.com/sites/Team/SitePages"
PAGE = f"{SITE}/Sales-Rally-Q2-'2025.aspx"
MARKUP = f'<a href="{PAGE}" originalsrc="{PAGE}">the rally</a>'


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _email(conn, message_id, html=MARKUP):
    email_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, content)"
        " VALUES (?, '2026-07-01', 'news', 'the rally')",
        (message_id,),
    ).lastrowid
    save_html(conn, email_id, html)
    conn.commit()


def _links(conn):
    return {
        url: (status, fetched is not None, attempts)
        for url, status, fetched, attempts in conn.execute(
            "SELECT url, last_status, fetched_at, attempts FROM sharepoint_links"
        )
    }


def _given_up(conn, url, message_id="AAMk-1", status="stale"):
    """A never-fetched link past its attempt budget, as the nightly pass leaves it."""
    for _ in range(6):
        record_link_in_db(conn, url=url, message_id=message_id, status=status)


def test_a_link_the_email_no_longer_yields_is_replaced_by_what_it_does(conn):
    _email(conn, "AAMk-1")
    _given_up(conn, f"{SITE}/Sales-Rally-Q2-")

    stats = rescan_links(conn, apply=True)

    assert stats == {"emails": 1, "dropped": 1, "added": 1}
    assert _links(conn) == {PAGE: ("queued", False, 0)}
    assert [u for u, _m in retry_candidates(conn)] == [PAGE]


def test_an_entity_left_in_a_link_is_dropped_when_the_clean_one_is_held(conn):
    _email(conn, "AAMk-1", html=f'<a href="{SITE}/Economy-&amp;-Markets.aspx">x</a>')
    record_link_in_db(conn, url=f"{SITE}/Economy-&-Markets.aspx", message_id="AAMk-1", status="ok")
    _given_up(conn, f"{SITE}/Economy-&amp;-Markets.aspx")

    stats = rescan_links(conn, apply=True)

    assert (stats["dropped"], stats["added"]) == (1, 0)
    assert list(_links(conn)) == [f"{SITE}/Economy-&-Markets.aspx"]


def test_a_link_the_email_still_yields_is_kept(conn):
    """A real 404: the document is gone, the link is not the scanner's."""
    _email(conn, "AAMk-1")
    _given_up(conn, PAGE)

    assert rescan_links(conn, apply=True)["dropped"] == 0
    assert _links(conn)[PAGE][0] == "stale"


def test_a_link_whose_email_is_gone_is_kept(conn):
    _given_up(conn, f"{SITE}/Sales-Rally-Q2-", message_id="AAMk-deleted")

    assert rescan_links(conn, apply=True) == {"emails": 1, "dropped": 0, "added": 0}
    assert f"{SITE}/Sales-Rally-Q2-" in _links(conn)


def test_a_stub_settled_as_not_content_gives_up_the_page_behind_it(conn):
    """A truncated page link has no .aspx, so link_kind read it as nothing to fetch."""
    _email(conn, "AAMk-1")
    record_link_in_db(
        conn, url=f"{SITE}/Sales-Rally-Q2-", message_id="AAMk-1", status="not-content"
    )

    stats = rescan_links(conn, apply=True)

    assert (stats["dropped"], stats["added"]) == (1, 1)
    assert _links(conn) == {PAGE: ("queued", False, 0)}


def test_a_link_that_holds_a_document_is_never_dropped(conn):
    _email(conn, "AAMk-1", html="<p>no links left</p>")
    record_link_in_db(
        conn,
        url=f"{SITE}/Old.aspx?x=1&amp%3Be=2",
        message_id="AAMk-1",
        status="not-content",
        document_message_id=-42,
    )
    record_link_in_db(conn, url=f"{SITE}/Fetched.aspx&quot", message_id="AAMk-1", status="ok")

    assert rescan_links(conn, apply=True)["dropped"] == 0
    assert len(_links(conn)) == 2


def test_a_not_content_link_the_email_yields_is_recorded_settled(conn):
    """What the retry pass would do with it anyway, without a fetch."""
    view = "https://contoso-my.sharepoint.com/personal/ann/_layouts/15/onedrive.aspx"
    _email(conn, "AAMk-1", html=f'<a href="{view}">files</a>')
    _given_up(conn, f"{view}&quot")

    rescan_links(conn, apply=True)

    assert _links(conn) == {view: ("not-content", True, 0)}


def test_the_dry_run_counts_and_changes_nothing(conn):
    _email(conn, "AAMk-1")
    _given_up(conn, f"{SITE}/Sales-Rally-Q2-")
    before = _links(conn)

    stats = rescan_links(conn, apply=False)

    assert (stats["dropped"], stats["added"]) == (1, 1)
    assert _links(conn) == before


# --- Pages recorded 'ok' that hold no page text ---------------------------------------------


def _page_document(conn, url, title="Launch"):
    from src.export.sharepoint_fetcher import SharepointPageResult
    from src.extract.sharepoint_ingest import ingest_page

    page = SharepointPageResult(
        url=url, status="ok", path="/sites/Team/SitePages/Launch.aspx", title=title, html=""
    )
    return ingest_page(conn, page, "2026-07-01")


def _shell_document(conn):
    """What the old `sharepoint-cli get` path made of a page: its script shell, as a file."""
    from src.extract.attachment_pipeline import ingest_text_document

    return ingest_text_document(
        conn,
        source="sharepoint",
        key="shell",
        filename="Launch.aspx",
        mime_type="application/octet-stream",
        text=None,
        sha256="ab" * 32,
        method=None,
        status="skipped",
        error="Unsupported type",
        subject="[SharePoint] shell",
        sender_name="SharePoint",
        date="2026-07-01",
    )["message_id"]


def test_pages_that_hold_no_page_text_are_offered_again(conn):
    launch = f"{SITE}/Launch.aspx"
    record_link_in_db(conn, url=f"{launch}?e=1", message_id="AAMk-1", status="ok")
    shell = _shell_document(conn)
    record_link_in_db(
        conn, url=f"{launch}?e=2", message_id="AAMk-1", status="ok", document_message_id=shell
    )
    held = f"{SITE}/Held.aspx"
    page_text = _page_document(conn, held)
    record_link_in_db(
        conn, url=held, message_id="AAMk-1", status="ok", document_message_id=page_text
    )
    plan = "https://contoso.sharepoint.com/sites/Team/Shared/plan.pdf"
    record_link_in_db(conn, url=plan, message_id="AAMk-1", status="ok")

    stats = reread_pages(conn, apply=True)

    assert stats == {"links": 2, "pages": 1}
    offered = sorted(u for u, _m in retry_candidates(conn))
    assert offered == [f"{launch}?e=1", f"{launch}?e=2"]


def test_the_page_dry_run_changes_nothing(conn):
    record_link_in_db(conn, url=f"{SITE}/Launch.aspx", message_id="AAMk-1", status="ok")

    assert reread_pages(conn, apply=False) == {"links": 1, "pages": 1}
    assert retry_candidates(conn) == []


def test_the_cli_repairs_both_and_fetches_nothing(tmp_path, monkeypatch, capsys):
    from src import cli
    from src.export import sharepoint_fetcher

    db = tmp_path / "brain.db"
    c = create_database(str(db))
    _email(c, "AAMk-1")
    _given_up(c, f"{SITE}/Sales-Rally-Q2-")
    record_link_in_db(c, url=f"{SITE}/Launch.aspx", message_id="AAMk-1", status="ok")
    c.close()
    for name in ("fetch_sharepoint_link", "fetch_sharepoint_page"):
        monkeypatch.setattr(sharepoint_fetcher, name, lambda *a, **k: pytest.fail("fetched"))

    rc = cli.cmd_process_sharepoint(
        Namespace(db=db, dry_run=False, since=None, limit=0, repair_links=True)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "1 dropped" in out and "1 recorded" in out and "1 link(s) to 1 page(s)" in out


# --- Links our own tenant refuses: what the owner asks access to ---------------------------


def test_refused_links_are_listed_once_per_document_with_their_emails(conn):
    _email(conn, "AAMk-1")
    _email(conn, "AAMk-2")
    sheet = "https://contoso.sharepoint.com/:x:/g/personal/ann/EQsheet"
    record_link_in_db(conn, url=sheet, message_id="AAMk-1", status="http-error")
    record_link_in_db(conn, url=f"{sheet}?xsdata=abc", message_id="AAMk-2", status="http-error")
    record_link_in_db(conn, url=f"{SITE}/Gone.aspx", message_id="AAMk-1", status="stale")

    docs = refused_links(conn)

    assert len(docs) == 1
    assert docs[0]["url"] == sheet
    assert docs[0]["links"] == 2
    assert [e["message_id"] for e in docs[0]["emails"]] == ["AAMk-1", "AAMk-2"]


def test_viewer_links_to_two_files_are_two_documents(conn):
    viewer = "https://contoso.sharepoint.com/sites/team/_layouts/15/Doc.aspx"
    for doc in ("A", "B"):
        record_link_in_db(
            conn, url=f"{viewer}?sourcedoc=%7B{doc}%7D", message_id="AAMk-1", status="http-error"
        )

    assert len(refused_links(conn)) == 2


def test_the_refused_list_runs_on_a_replica(tmp_path, monkeypatch, capsys):
    import sys

    from src.cli import main

    db = tmp_path / "brain.db"
    c = create_database(str(db))
    record_link_in_db(
        c,
        url="https://contoso.sharepoint.com/:b:/g/EQpdf",
        message_id="AAMk-1",
        status="http-error",
    )
    c.close()
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "sharepoint-refused"])

    main()

    assert "https://contoso.sharepoint.com/:b:/g/EQpdf" in capsys.readouterr().out

"""The hollow SharePoint documents an old fetcher left: retired when their content is held or
there is none, their links queued again when it is not.

1,106 of the producer's 3,680 standalone documents held no text (audit 2026-10-16). Each was a
"SharePoint page shell": before 2026-09-30 a link was fetched as a file, and a page, a home page
or a viewer answered with its page frame, which the backlog pass of 2026-09-30 stored as a
document with no text and a one-off script labelled. The repair of that week read the pages
again into documents of their own and pointed the links at them, and left the shells. The fetch
of today makes none: a page is read as text and a file that yields none makes no document. The
command finds each shell, decides by its links what it stands for, and with --apply forgets the
ones whose content is held or that have none, and queues for the retry pass the links whose
content is not held.
"""

import hashlib

import pytest

from src.export.sharepoint_fetcher import record_link_in_db
from src.export.sharepoint_shells import SHELL_REASON, classify, retire
from src.extract.attachment_pipeline import ingest_text_document
from src.store.schema import create_database

HOST = "contoso.sharepoint.com"
PAGE = f"https://{HOST}/sites/Team/SitePages/Card-launch.aspx"
SETTINGS = f"https://{HOST}/_layouts/15/sharepoint.aspx/emailNotificationSettings"
FILE = f"https://{HOST}/:x:/s/Team/IQAbCdEf"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("src.store.embeddings.EMBEDDINGS_FILE", tmp_path / "embeddings.npz")
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _document(conn, key, subject, text=None, error=SHELL_REASON, source="sharepoint") -> int:
    out = ingest_text_document(
        conn,
        source=source,
        key=key,
        filename=key[:20],
        mime_type="application/octet-stream",
        text=text,
        sha256=hashlib.sha256(key.encode()).hexdigest(),
        method=None if text is None else "sharepoint-page",
        status="skipped" if text is None else "extracted",
        error=None if text else error,
        subject=subject,
        sender_name="SharePoint",
        date="2026-06-01T08:00:00Z",
    )
    return out["message_id"]


def _shell(conn, key, url_label):
    return _document(conn, key, f"[SharePoint] {url_label}")


def _link(conn, url, status, document=None):
    record_link_in_db(
        conn, url=url, message_id="AAMk-1", status=status, document_message_id=document
    )


def _documents(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM emails WHERE mailbox_name = 'External'").fetchone()[0]


def test_a_shell_whose_page_is_held_is_retired(conn):
    page = _document(
        conn,
        "/sites/team/sitepages/card-launch.aspx",
        "[SharePoint page] Launch",
        text="The card launches in June.",
        source="sharepoint-page",
    )
    shell = _shell(conn, "aa1", f"{HOST}/sites/Team/SitePages/Card-launch.aspx")
    _link(conn, PAGE, "ok", page)

    found = classify(conn)
    assert found["held"] == [shell]

    stats = retire(conn, found)

    assert stats["forgotten"] == 1 and _documents(conn) == 1  # the page stays
    assert conn.execute("SELECT document_message_id FROM sharepoint_links").fetchone()[0] == page


def test_a_shell_of_a_link_with_nothing_to_read_is_retired(conn):
    shell = _shell(conn, "aa2", f"{HOST}/_layouts/15/sharepoint.aspx/emailNotificationSettings")
    _link(conn, SETTINGS, "not-content", shell)

    found = classify(conn)
    assert found["not_content"] == [shell]

    retire(conn, found)

    assert _documents(conn) == 0
    row = conn.execute("SELECT last_status, document_message_id FROM sharepoint_links").fetchone()
    assert tuple(row) == ("not-content", None)  # the link no longer names a forgotten document


def test_a_shell_whose_content_is_not_held_has_its_link_queued_and_stays(conn):
    shell = _shell(conn, "aa3", f"{HOST}/:x:/s/Team/IQAbCdEf")
    _link(conn, FILE, "ok", shell)

    found = classify(conn)
    assert found["refetch"] == [shell]

    stats = retire(conn, found)

    assert stats["links_queued"] == 1 and _documents(conn) == 1
    row = conn.execute(
        "SELECT fetched_at, attempts, document_message_id FROM sharepoint_links"
    ).fetchone()
    assert tuple(row) == (None, 0, None)  # the retry pass fetches it (retry_candidates)


def test_a_shell_no_link_names_is_retired(conn):
    shell = _shell(conn, "aa4", "sharepoint-download (31)")

    found = classify(conn)

    assert found["orphan"] == [shell]
    retire(conn, found)
    assert _documents(conn) == 0


def test_a_shell_whose_link_is_on_another_tenant_cannot_be_fetched_and_is_retired(conn):
    shell = _shell(conn, "aa5", "partner.sharepoint.com/sites/X/SitePages/P.aspx")
    _link(conn, "https://partner.sharepoint.com/sites/X/SitePages/P.aspx", "unsupported-host")

    assert classify(conn)["orphan"] == [shell]


def test_only_shells_are_judged(conn):
    _document(conn, "aa6", "[SharePoint] a protected file", error="IRM-protected .docx: no text")
    _document(conn, "aa7", "[SharePoint page] News", text="News text", source="sharepoint-page")

    found = classify(conn)

    assert sum(len(ids) for ids in found.values()) == 0


def test_the_command_is_a_dry_run_unless_told(monkeypatch, capsys, tmp_path, conn):
    from tests.brain_cli import brain

    _shell(conn, "aa8", "sharepoint-download (2)")
    conn.commit()
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    db = str(tmp_path / "brain.db")

    code, out, _ = brain(monkeypatch, capsys, "--db", db, "maint", "sharepoint-shells")
    assert code == 0 and "DRY RUN" in out and "no link names it" in out
    assert _documents(conn) == 1

    code, out, _ = brain(monkeypatch, capsys, "--db", db, "maint", "sharepoint-shells", "--apply")
    assert code == 0 and "forgotten" in out
    assert _documents(conn) == 0

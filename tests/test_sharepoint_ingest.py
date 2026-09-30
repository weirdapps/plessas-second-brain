"""SharePoint files become text-only documents, and the fetched file is never kept.

A link is fetched into a temporary directory and stored as text. The bytes' hash is the
document's identity, so a file linked from two emails is one document. Files that earlier
fetches left under data/sharepoint are ingested by the backlog pass and deleted by the sweep
once their text is stored. Those fetches were saved flat by name, so a later file could replace
an earlier one: a link whose file is gone or changed size is offered for fetching again.
"""

from argparse import Namespace
from pathlib import Path

import pytest

from src.export import sharepoint_fetcher
from src.export.sharepoint_fetcher import SharepointFetchResult, record_link_in_db
from src.extract import sharepoint_ingest
from src.extract.sharepoint_ingest import fetch_and_ingest, ingest_fetched_backlog
from src.store.file_sweep import (
    DELETABLE,
    PENDING_TEXT,
    SweepPolicy,
    classify_sharepoint_files,
    sweep_files,
)
from src.store.schema import create_database

URL = "https://tenant.example.com/sites/Team/Shared%20Documents/Plan.txt"
WORDS = "The plan for next year, in enough words to pass the noise filter easily."


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    c.execute(
        "INSERT INTO emails (message_id, date_received, subject)"
        " VALUES ('AAMk-1', '2026-03-01', 'see the plan')"
    )
    c.commit()
    yield c
    c.close()


def _fake_fetch(monkeypatch, body=WORDS, status="ok", seen=None, name="Plan.txt"):
    def fake(url, out_dir, managed_host=None):
        if seen is not None:
            seen.append(Path(out_dir))
        if status != "ok":
            return SharepointFetchResult(url=url, status=status, error_message="refused")
        f = Path(out_dir) / name
        f.write_text(body)
        return SharepointFetchResult(
            url=url, status="ok", local_path=f, file_name=f.name, file_size=f.stat().st_size
        )

    monkeypatch.setattr(sharepoint_fetcher, "fetch_sharepoint_link", fake)


def _text_of(conn, message_id):
    return conn.execute(
        "SELECT ac.extracted_text FROM attachment_content ac"
        " JOIN attachments a ON a.id = ac.attachment_id WHERE a.message_id = ?",
        (message_id,),
    ).fetchone()[0]


def test_a_fetch_keeps_no_file_and_stores_the_text(conn, monkeypatch):
    seen: list = []
    _fake_fetch(monkeypatch, seen=seen)

    result, document = fetch_and_ingest(conn, URL, "AAMk-1")

    assert result.status == "ok"
    assert not seen[0].exists()
    subject, date = conn.execute(
        "SELECT subject, date_received FROM emails WHERE message_id = ?", (document,)
    ).fetchone()
    assert subject == "[SharePoint] tenant.example.com/sites/Team/Shared Documents/Plan.txt"
    assert date == "2026-03-01"
    assert "plan for next year" in _text_of(conn, document)


def test_a_file_linked_from_two_emails_is_one_document(conn, monkeypatch):
    _fake_fetch(monkeypatch)

    _, first = fetch_and_ingest(conn, URL, "AAMk-1")
    _, second = fetch_and_ingest(conn, URL + "?web=1", "AAMk-1")

    assert first == second
    count = conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE file_path LIKE 'text:sharepoint:%'"
    ).fetchone()[0]
    assert count == 1


def test_a_failed_fetch_stores_nothing(conn, monkeypatch):
    _fake_fetch(monkeypatch, status="http-error")

    result, document = fetch_and_ingest(conn, URL, "AAMk-1")

    assert (result.status, document) == ("http-error", None)
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_the_link_records_its_document(conn, monkeypatch):
    _fake_fetch(monkeypatch)
    result, document = fetch_and_ingest(conn, URL, "AAMk-1")

    record_link_in_db(
        conn,
        url=URL,
        message_id="AAMk-1",
        status=result.status,
        file_name=result.file_name,
        file_size=result.file_size,
        document_message_id=document,
    )
    record_link_in_db(conn, url=URL, message_id="AAMk-1", status="http-error")

    held = conn.execute(
        "SELECT document_message_id FROM sharepoint_links WHERE url = ?", (URL,)
    ).fetchone()[0]
    assert held == document, "a later failed attempt must not erase the document"


def test_the_backlog_is_ingested_and_linked(conn, tmp_path):
    root = tmp_path / "sharepoint"
    root.mkdir()
    kept = root / "Plan.txt"
    kept.write_text(WORDS)
    (root / "Other.txt").write_text(WORDS + " Other.")
    record_link_in_db(
        conn,
        url=URL,
        message_id="AAMk-1",
        status="ok",
        fetched_path=str(kept),
        file_name="Plan.txt",
        file_size=kept.stat().st_size,
    )

    stats = ingest_fetched_backlog(conn, [root])

    assert (stats["files"], stats["ingested"], stats["linked"]) == (2, 2, 1)
    doc = conn.execute(
        "SELECT document_message_id FROM sharepoint_links WHERE url = ?", (URL,)
    ).fetchone()[0]
    assert "plan for next year" in _text_of(conn, doc)
    assert kept.exists(), "the backlog pass stores text; the sweep deletes files"


def test_a_second_backlog_pass_extracts_nothing_again(conn, tmp_path, monkeypatch):
    root = tmp_path / "sharepoint"
    root.mkdir()
    (root / "Plan.txt").write_text(WORDS)
    ingest_fetched_backlog(conn, [root])
    monkeypatch.setattr(
        sharepoint_ingest, "extract_text_from_file", lambda *a: pytest.fail("extracted twice")
    )

    assert ingest_fetched_backlog(conn, [root])["already"] == 1


def test_a_link_whose_file_is_gone_or_replaced_is_offered_again(conn, tmp_path):
    root = tmp_path / "sharepoint"
    root.mkdir()
    replaced = root / "Plan.txt"
    replaced.write_text(WORDS)
    record_link_in_db(
        conn,
        url=URL,
        message_id="AAMk-1",
        status="ok",
        fetched_path=str(replaced),
        file_name="Plan.txt",
        file_size=replaced.stat().st_size + 7,
    )
    gone = URL.replace("Plan", "Gone")
    record_link_in_db(
        conn,
        url=gone,
        message_id="AAMk-1",
        status="ok",
        fetched_path=str(root / "Gone.txt"),
        file_name="Gone.txt",
        file_size=10,
    )

    stats = ingest_fetched_backlog(conn, [root])

    assert stats["refetch"] == 2
    for url in (URL, gone):
        row = conn.execute(
            "SELECT fetched_at, attempts FROM sharepoint_links WHERE url = ?", (url,)
        ).fetchone()
        assert tuple(row) == (None, 0)


def test_the_sweep_holds_a_sharepoint_file_until_its_text_is_stored(conn, tmp_path):
    root = tmp_path / "sharepoint"
    root.mkdir()
    (root / "Plan.txt").write_text(WORDS)
    assert [f.state for f in classify_sharepoint_files(conn, root)] == [PENDING_TEXT]

    ingest_fetched_backlog(conn, [root])

    assert [f.state for f in classify_sharepoint_files(conn, root)] == [DELETABLE]


def test_the_sweep_deletes_stored_sharepoint_files_and_keeps_the_directory(conn, tmp_path):
    root = tmp_path / "sharepoint"
    root.mkdir()
    f = root / "Plan.txt"
    f.write_text(WORDS)
    ingest_fetched_backlog(conn, [root])

    stats = sweep_files(
        conn, tmp_path / "attachments", SweepPolicy(apply=True), sharepoint_root=root
    )

    assert stats["deleted"] == 1
    assert not f.exists()
    assert root.is_dir()


def test_a_text_only_image_is_not_queued_for_vision(conn, tmp_path):
    from src.extract.image_pipeline import run_backfill

    img = tmp_path / "sharepoint" / "chart.png"
    img.parent.mkdir()
    img.write_bytes(b"\x89PNG not really")
    sharepoint_ingest.ingest_fetched_file(conn, img, URL.replace("Plan.txt", "chart.png"))

    assert run_backfill(conn, unprocessed_only=True, dry_run=True)["scanned"] == 0


def test_the_retry_pass_stops_at_max_fetches(tmp_path, monkeypatch):
    from src import cli

    db = tmp_path / "brain.db"
    c = create_database(str(db))
    for i in range(3):
        record_link_in_db(
            c, url=URL.replace("Plan.txt", f"Plan{i}.txt"), message_id="AAMk-1", status="http-error"
        )
    c.close()
    monkeypatch.setenv("SHAREPOINT_HOST", "tenant.example.com")
    monkeypatch.setattr("src.config.SHAREPOINT_HOST", "tenant.example.com")
    calls: list = []
    monkeypatch.setattr(
        sharepoint_ingest,
        "fetch_and_ingest",
        lambda conn, url, message_id: (
            calls.append(url)
            or (SharepointFetchResult(url=url, status="http-error", error_message="x"), None)
        ),
    )

    cli.cmd_process_sharepoint(
        Namespace(db=db, dry_run=False, since=None, limit=0, max_fetches=2, ingest_fetched=False)
    )

    assert len(calls) == 2


def test_the_mcp_refetch_keeps_no_file(conn, monkeypatch):
    from src import config, mcp_server

    record_link_in_db(conn, url=URL, message_id="AAMk-1", status="http-error")
    monkeypatch.setattr(config, "SHAREPOINT_HOST", "tenant.example.com")
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)
    seen: list = []
    _fake_fetch(monkeypatch, seen=seen)

    out = mcp_server.sharepoint_index("refetch", url=URL)

    assert out["status"] == "ok"
    assert out["document_message_id"]
    assert not seen[0].exists()


def test_no_fetch_starts_once_the_time_budget_is_spent(tmp_path, monkeypatch):
    """Each fetch now extracts too, and the nightly pass has an hour for every stage: a spent
    budget stops the SharePoint stage instead of the unit being killed before the sweep."""
    from src import cli

    db = tmp_path / "brain.db"
    c = create_database(str(db))
    for i in range(3):
        record_link_in_db(c, url=f"{URL}?n={i}", message_id="AAMk-1", status="http-error")
    c.close()
    monkeypatch.setenv("SHAREPOINT_HOST", "tenant.example.com")
    monkeypatch.setattr("src.config.SHAREPOINT_HOST", "tenant.example.com")
    calls: list = []
    monkeypatch.setattr(
        sharepoint_ingest,
        "fetch_and_ingest",
        lambda conn, url, message_id: (
            calls.append(url)
            or (SharepointFetchResult(url=url, status="http-error", error_message="x"), None)
        ),
    )

    cli.cmd_process_sharepoint(
        Namespace(
            db=db,
            dry_run=False,
            since=None,
            limit=0,
            max_fetches=0,
            ingest_fetched=False,
            deadline_s=0,
        )
    )

    assert calls == []


def test_the_nightly_pass_gives_the_sharepoint_stage_a_budget():
    wrapper = (
        Path(__file__).resolve().parent.parent / "scripts/wrappers/systemd/sb-attachment-pass.sh"
    )
    line = next(ln for ln in wrapper.read_text().splitlines() if "process-sharepoint" in ln)
    assert "--deadline-s" in line


def test_a_fetched_page_with_nothing_to_extract_makes_no_document(conn, monkeypatch):
    """A view link can return the browser page instead of the file (an .aspx viewer, a sign-in
    or error page). There is no text to store, so no document is made; the link still counts
    as fetched, so it is not retried every night."""
    page = "<!DOCTYPE html><html><head><script>var app = {};</script></head><body></body></html>"
    _fake_fetch(monkeypatch, body=page, name="Doc.aspx")

    result, document = fetch_and_ingest(conn, URL, "AAMk-1")

    assert (result.status, document) == ("ok", None)
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


PAGE = "https://contoso.sharepoint.com/sites/News/SitePages/Launch.aspx?e=1"
PAGE_HTML = (
    "<div><h1>Launch</h1><p>The launch plan for next year, in enough words to pass.</p></div>"
)


def _fake_page(monkeypatch, html=PAGE_HTML, title="Launch", status="ok", calls=None):
    from src.export.sharepoint_fetcher import SharepointPageResult

    def fake(url, managed_host=None):
        if calls is not None:
            calls.append(url)
        if status != "ok":
            return SharepointPageResult(url=url, status=status, http_status=500, error_message="x")
        return SharepointPageResult(
            url=url, status="ok", path="/sites/News/SitePages/Launch.aspx", title=title, html=html
        )

    monkeypatch.setattr(sharepoint_fetcher, "fetch_sharepoint_page", fake)


def _no_file_fetch(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a file fetch was started")

    monkeypatch.setattr(sharepoint_fetcher, "fetch_sharepoint_link", refuse)


def test_a_page_becomes_a_document_with_its_title_and_text(conn, monkeypatch):
    _fake_page(monkeypatch)
    _no_file_fetch(monkeypatch)

    result, document = fetch_and_ingest(conn, PAGE, "AAMk-1")

    assert result.status == "ok"
    subject, date = conn.execute(
        "SELECT subject, date_received FROM emails WHERE message_id = ?", (document,)
    ).fetchone()
    assert (subject, date) == ("[SharePoint page] Launch", "2026-03-01")
    text = _text_of(conn, document)
    assert "launch plan for next year" in text
    assert "<p>" not in text
    file_path = conn.execute(
        "SELECT file_path FROM attachments WHERE message_id = ?", (document,)
    ).fetchone()[0]
    assert file_path == "text:sharepoint-page:/sites/news/sitepages/launch.aspx"


def test_a_page_linked_from_two_emails_is_one_document(conn, monkeypatch):
    _fake_page(monkeypatch)

    _, first = fetch_and_ingest(conn, PAGE, "AAMk-1")
    _, second = fetch_and_ingest(conn, PAGE.replace("?e=1", "?e=2"), "AAMk-1")

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_a_page_with_no_text_makes_no_document(conn, monkeypatch):
    _fake_page(monkeypatch, html="<div><img src='banner.png'></div>")

    result, document = fetch_and_ingest(conn, PAGE, "AAMk-1")

    assert (result.status, document) == ("ok", None)
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_a_page_that_fails_to_read_carries_its_status(conn, monkeypatch):
    _fake_page(monkeypatch, status="http-error")

    result, document = fetch_and_ingest(conn, PAGE, "AAMk-1")

    assert (result.status, result.http_status, document) == ("http-error", 500, None)


def test_a_link_that_is_not_content_is_not_fetched(conn, monkeypatch):
    calls: list = []
    _fake_page(monkeypatch, calls=calls)
    _no_file_fetch(monkeypatch)

    result, document = fetch_and_ingest(
        conn, "https://contoso-my.sharepoint.com/personal/ann/_layouts/15/onedrive.aspx", "AAMk-1"
    )

    assert (result.status, document, calls) == ("not-content", None, [])


def _process(db, **overrides):
    from src import cli

    args = {
        "db": db,
        "dry_run": False,
        "since": None,
        "limit": 0,
        "max_fetches": 0,
        "ingest_fetched": False,
        "deadline_s": None,
        "refetch_content": False,
    }
    args.update(overrides)
    return cli.cmd_process_sharepoint(Namespace(**args))


@pytest.fixture
def tenant(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_HOST", "contoso.sharepoint.com")
    monkeypatch.setattr("src.config.SHAREPOINT_HOST", "contoso.sharepoint.com")


def _links(db):
    import sqlite3

    c = sqlite3.connect(str(db))
    try:
        return {
            url: (status, document)
            for url, status, document in c.execute(
                "SELECT url, last_status, document_message_id FROM sharepoint_links"
            )
        }
    finally:
        c.close()


def test_links_to_one_page_are_fetched_once_per_run(conn, tmp_path, tenant, monkeypatch):
    for query in ("e=1", "e=2"):
        record_link_in_db(conn, url=PAGE.replace("e=1", query), message_id="AAMk-1", status="stale")
    calls: list = []
    _fake_page(monkeypatch, calls=calls)

    _process(tmp_path / "brain.db")

    assert len(calls) == 1
    outcomes = set(_links(tmp_path / "brain.db").values())
    assert len(outcomes) == 1
    status, document = outcomes.pop()
    assert status == "ok" and document


def _seed_ok(conn, url, text=None, document=True):
    """An 'ok' link as an earlier fetch left it: its document holds `text`, or it has none."""
    import hashlib

    from src.extract.attachment_pipeline import ingest_text_document

    doc = None
    if document:
        doc = ingest_text_document(
            conn,
            source="sharepoint",
            key=url,
            filename="Doc.aspx",
            mime_type="text/html",
            text=text,
            sha256=hashlib.sha256(url.encode()).hexdigest(),
            method="html",
            status="extracted" if text else "skipped",
            error=None,
            subject="[SharePoint] earlier fetch",
            sender_name="SharePoint",
            date="2026-03-01",
        )["message_id"]
    record_link_in_db(conn, url=url, message_id="AAMk-1", status="ok", document_message_id=doc)
    conn.execute(
        "UPDATE sharepoint_links SET last_attempt_at = '2026-09-01T00:00:00+00:00' WHERE url = ?",
        (url,),
    )
    conn.commit()


def test_the_refetch_rereads_each_link_that_holds_no_text_once(conn, tmp_path, tenant, monkeypatch):
    empty_file = "https://contoso.sharepoint.com/:w:/g/sites/team/EQdoc"
    with_text = "https://contoso.sharepoint.com/:x:/g/sites/team/EQxls"
    settings = "https://contoso-my.sharepoint.com/personal/ann/_layouts/15/onedrive.aspx"
    _seed_ok(conn, PAGE)
    _seed_ok(conn, PAGE.replace("e=1", "e=2"))
    _seed_ok(conn, empty_file, document=False)
    _seed_ok(conn, with_text, text=WORDS)
    _seed_ok(conn, settings)
    page_calls: list = []
    file_calls: list = []
    _fake_page(monkeypatch, calls=page_calls)
    _fake_fetch(monkeypatch, seen=file_calls)

    _process(tmp_path / "brain.db", refetch_content=True)

    assert (len(page_calls), len(file_calls)) == (1, 1)
    links = _links(tmp_path / "brain.db")
    assert links[settings][0] == "not-content"
    assert "launch plan" in _text_of(conn, links[PAGE][1])
    assert links[PAGE] == links[PAGE.replace("e=1", "e=2")]
    assert "plan for next year" in _text_of(conn, links[empty_file][1])

    _process(tmp_path / "brain.db", refetch_content=True)

    assert (len(page_calls), len(file_calls)) == (1, 1)


def test_a_refetch_stopped_early_resumes_where_it_stopped(conn, tmp_path, tenant, monkeypatch):
    for name in ("EQone", "EQtwo"):
        _seed_ok(conn, f"https://contoso.sharepoint.com/:w:/g/sites/team/{name}", document=False)
    calls: list = []
    _fake_fetch(monkeypatch, body="", seen=calls)  # still no text: only the resume mark ends it

    _process(tmp_path / "brain.db", refetch_content=True, max_fetches=1)
    _process(tmp_path / "brain.db", refetch_content=True, max_fetches=1)
    _process(tmp_path / "brain.db", refetch_content=True, max_fetches=1)

    assert len(calls) == 2


def test_viewer_links_on_one_web_each_fetch_their_own_file(conn, tmp_path, tenant, monkeypatch):
    viewer = "https://contoso.sharepoint.com/sites/team/_layouts/15/Doc.aspx"
    plan, budget = f"{viewer}?sourcedoc=%7BA%7D&file=Plan.docx", f"{viewer}?sourcedoc=%7BB%7D"
    bodies = {plan: WORDS, budget: "The budget for next year, in enough words to pass the filter."}
    for url in (plan, budget):
        record_link_in_db(conn, url=url, message_id="AAMk-1", status="stale")
    calls: list = []

    def fake(url, out_dir, managed_host=None):
        calls.append(url)
        f = Path(out_dir) / "file.txt"
        f.write_text(bodies[url])
        return SharepointFetchResult(url=url, status="ok", local_path=f, file_name=f.name)

    monkeypatch.setattr(sharepoint_fetcher, "fetch_sharepoint_link", fake)

    _process(tmp_path / "brain.db")

    links = _links(tmp_path / "brain.db")
    assert sorted(calls) == sorted([plan, budget])
    assert links[plan][1] != links[budget][1]
    assert "budget for next year" in _text_of(conn, links[budget][1])


def test_a_link_that_is_not_content_is_not_counted_as_failed(
    conn, tmp_path, tenant, monkeypatch, capsys
):
    view = "https://contoso-my.sharepoint.com/personal/ann/_layouts/15/onedrive.aspx"
    record_link_in_db(conn, url=view, message_id="AAMk-1", status="stale")
    _no_file_fetch(monkeypatch)

    _process(tmp_path / "brain.db")

    out = capsys.readouterr().out
    assert "URLs failed: 0" in out
    assert "Not content (not fetched): 1" in out

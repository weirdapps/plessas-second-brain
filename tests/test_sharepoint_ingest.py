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


def _fake_fetch(monkeypatch, body=WORDS, status="ok", seen=None):
    def fake(url, out_dir, managed_host=None):
        if seen is not None:
            seen.append(Path(out_dir))
        if status != "ok":
            return SharepointFetchResult(url=url, status=status, error_message="refused")
        f = Path(out_dir) / "Plan.txt"
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

"""`brain process-sharepoint` stage by stage: the retry pass, then the scan, and the modes
that fetch nothing.

sharepoint-cli is a fake that answers each URL with a status; the store is a temporary
one. These read the orchestrator alone (src/pipeline/sharepoint.py).
"""

from argparse import Namespace
from unittest.mock import patch

import pytest

from src import config
from src.export.sharepoint_fetcher import SharepointFetchResult, record_link_in_db
from src.pipeline import EXIT_REAUTH, sharepoint
from src.store.schema import create_database, get_connection

MARK = "sharepoint_scan_last_id"


@pytest.fixture(autouse=True)
def _tenant(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_HOST", "contoso.sharepoint.com")


def _url(n: int) -> str:
    return f"https://contoso.sharepoint.com/:w:/g/sites/Team/Edoc{n}"


def _store(tmp_path, *contents: str):
    """A store with one email per content, in order; (path, their ids)."""
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    ids = []
    for n, content in enumerate(contents):
        ids.append(
            conn.execute(
                "INSERT INTO emails (message_id, date_received, content, mailbox_name) "
                "VALUES (?, ?, ?, 'Archive')",
                (f"m-{n}", f"2026-09-{10 + n:02d}T09:00:00", content),
            ).lastrowid
        )
    conn.commit()
    conn.close()
    return db, ids


def _mark(db):
    conn = get_connection(str(db))
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (MARK,)).fetchone()
    conn.close()
    return int(row[0]) if row else None


def _run(db, status_for=lambda url: "ok", **options):
    """(exit code, the URLs fetched in order)."""
    args = {"db": str(db), "since": None, "limit": 0, "dry_run": False, **options}
    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as fetch:
        fetch.side_effect = lambda url, out_dir: SharepointFetchResult(
            url=url, status=status_for(url)
        )
        code = sharepoint.run(Namespace(**args))
    return code, [call.args[0] for call in fetch.call_args_list]


def test_the_retry_pass_comes_first_and_the_scan_skips_what_it_fetched(tmp_path):
    db, ids = _store(tmp_path, f"see {_url(1)} and {_url(2)}")
    conn = get_connection(str(db))
    record_link_in_db(conn, url=_url(1), message_id="m-0", status="http-error")
    conn.close()

    code, fetched = _run(db)

    assert code == 0
    assert fetched == [_url(1), _url(2)]
    assert _mark(db) == ids[-1]


def test_an_expired_session_stops_the_pass_and_asks_for_a_login(tmp_path):
    db, _ = _store(tmp_path, f"see {_url(1)}", f"see {_url(2)}")

    code, fetched = _run(db, status_for=lambda url: "auth-required")

    assert code == EXIT_REAUTH
    assert fetched == [_url(1)]
    assert _mark(db) is None


def test_stored_files_are_ingested_without_a_fetch_or_a_tenant(tmp_path, monkeypatch):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    monkeypatch.delenv("SHAREPOINT_HOST")
    roots = []
    monkeypatch.setattr(
        "src.extract.sharepoint_ingest.ingest_fetched_backlog",
        lambda conn, given: roots.append(given) or {"stored": 0},
    )

    code, fetched = _run(db, ingest_fetched=True)

    assert (code, fetched) == (0, [])
    assert roots == [
        [sharepoint.SHAREPOINT_DATA_DIR, config.ATTACHMENTS_DIR / "sharepoint-refetch"]
    ]


def test_repairing_links_queues_them_and_fetches_nothing(tmp_path, monkeypatch):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    applied = []
    monkeypatch.setattr(
        "src.export.sharepoint_repair.rescan_links",
        lambda conn, apply: (
            applied.append(("links", apply)) or {"emails": 0, "dropped": 0, "added": 0}
        ),
    )
    monkeypatch.setattr(
        "src.export.sharepoint_repair.reread_pages",
        lambda conn, apply: applied.append(("pages", apply)) or {"links": 0, "pages": 0},
    )

    code, fetched = _run(db, repair_links=True, dry_run=True)

    assert (code, fetched) == (0, [])
    assert applied == [("links", False), ("pages", False)]


def test_without_a_tenant_a_fetching_run_exits_2_before_any_fetch(tmp_path, monkeypatch):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    monkeypatch.delenv("SHAREPOINT_HOST")

    with pytest.raises(SystemExit) as exc:
        _run(db)

    assert exc.value.code == 2
    assert _mark(db) is None

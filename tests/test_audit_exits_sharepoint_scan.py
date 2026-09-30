"""process-sharepoint scans mail past a high-water mark (export-1, cli-1,
silent-failures-2).

The nightly pass read the 200 newest rows by date_received, News included,
while a weekday adds 400-500. Everything older than the last few hours of mail
was never scanned, so its links were never recorded, fetched or retried, and the
stage reported a clean run. The scan now starts past the highest emails.id it
has already read, skips News, and a per-run fetch cap keeps a backlog inside the
unit's timeout instead of the window doing it by accident.
"""

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from src import cli
from src.export.sharepoint_fetcher import SharepointFetchResult
from src.store.schema import create_database, get_connection

MARK_KEY = "sharepoint_scan_last_id"
WRAPPER = (
    Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-attachment-pass.sh"
)


@pytest.fixture(autouse=True)
def _configured_tenant(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_HOST", "contoso.sharepoint.com")


def _url(n: int) -> str:
    return f"https://contoso.sharepoint.com/:w:/g/sites/Team/Edoc{n}"


def _db(tmp_path, emails):
    """emails: (mailbox_name, content). Returns (db_path, [ids in insert order])."""
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    ids = []
    for i, (mailbox, content) in enumerate(emails):
        cur = conn.execute(
            "INSERT INTO emails (message_id, date_received, content, mailbox_name) "
            "VALUES (?, ?, ?, ?)",
            (f"m-{i}", f"2026-09-{10 + i:02d}T09:00:00", content, mailbox),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return db_path, ids


def _set_mark(db_path, value):
    conn = get_connection(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)", (MARK_KEY, str(value))
    )
    conn.commit()
    conn.close()


def _mark(db_path):
    conn = get_connection(str(db_path))
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (MARK_KEY,)).fetchone()
    conn.close()
    return int(row[0]) if row else None


def _run(db_path, status_for=None, **overrides):
    """Run the pass with the fetcher stubbed. Returns (rc, urls fetched in order)."""
    status_for = status_for or (lambda url: "ok")

    def fake_fetch(url, out_dir):
        return SharepointFetchResult(url=url, status=status_for(url))

    options = {"db": str(db_path), "since": None, "limit": 0, "dry_run": False}
    options.update(overrides)
    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as fetch:
        fetch.side_effect = fake_fetch
        rc = cli.cmd_process_sharepoint(argparse.Namespace(**options))
    return rc, [c.args[0] for c in fetch.call_args_list]


def test_with_no_mark_everything_is_scanned_and_the_mark_is_set(tmp_path):
    db_path, ids = _db(
        tmp_path,
        [("Archive", f"a {_url(1)} b"), ("Sent Items", f"c {_url(2)} d"), ("Inbox", "no link")],
    )

    _, fetched = _run(db_path)

    assert fetched == [_url(1), _url(2)]
    assert _mark(db_path) == ids[-1]


def test_rows_at_or_below_the_mark_are_not_rescanned(tmp_path):
    db_path, ids = _db(tmp_path, [("Archive", f"a {_url(1)} b"), ("Archive", f"c {_url(2)} d")])
    _set_mark(db_path, ids[0])

    _, fetched = _run(db_path)

    assert fetched == [_url(2)]
    assert _mark(db_path) == ids[1]


def test_news_is_skipped(tmp_path):
    db_path, _ = _db(tmp_path, [("News", f"digest {_url(1)}"), ("Archive", f"a {_url(2)} b")])

    _, fetched = _run(db_path)

    assert fetched == [_url(2)]


def test_the_mark_advances_across_runs(tmp_path):
    db_path, ids = _db(tmp_path, [("Archive", f"a {_url(1)} b")])
    _run(db_path)
    assert _mark(db_path) == ids[0]

    conn = get_connection(str(db_path))
    new_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, content, mailbox_name) "
        "VALUES ('m-late', '2026-01-01T00:00:00', ?, 'Archive')",
        (f"loaded late with an old date {_url(9)}",),
    ).lastrowid
    conn.commit()
    conn.close()

    _, fetched = _run(db_path)

    # An id mark also catches mail loaded late with an old date_received.
    assert fetched == [_url(9)]
    assert _mark(db_path) == new_id


def test_the_fetch_cap_stops_the_mark_before_the_email_it_could_not_finish(tmp_path):
    db_path, ids = _db(
        tmp_path,
        [
            ("Archive", f"a {_url(1)} b"),
            ("Archive", "nothing here"),
            ("Archive", f"c {_url(2)} {_url(3)} d"),
            ("Archive", f"e {_url(4)} f"),
        ],
    )

    _, fetched = _run(db_path, max_fetches=2)

    assert fetched == [_url(1), _url(2)]
    # The third email still has an unfetched link, so the next run starts there.
    assert _mark(db_path) == ids[1]

    _, fetched = _run(db_path, max_fetches=2)

    # _url(2) was fetched OK last night and is not fetched again.
    assert fetched == [_url(3), _url(4)]
    assert _mark(db_path) == ids[3]


def test_an_auth_break_does_not_advance_past_the_email_it_stopped_on(tmp_path):
    db_path, ids = _db(tmp_path, [("Archive", f"a {_url(1)} b"), ("Archive", f"c {_url(2)} d")])

    rc, _ = _run(db_path, status_for=lambda url: "auth-required" if url == _url(2) else "ok")

    assert rc == cli.EXIT_REAUTH
    assert _mark(db_path) == ids[0]


def test_a_dry_run_leaves_the_mark_alone(tmp_path):
    db_path, _ = _db(tmp_path, [("Archive", f"a {_url(1)} b")])

    _, fetched = _run(db_path, dry_run=True)

    assert fetched == []
    assert _mark(db_path) is None


# repo-autoupdate pulls new code onto the producer every hour but never copies
# the wrappers, which run from ~/.local/bin. Until one is copied by hand the
# nightly pass keeps calling "process-sharepoint --limit 200", so that line must
# behave like the new one: read every email past the mark, fetch a bounded
# number. 200 emails a night against 400-500 a weekday would have left the mark
# further behind every night.
def test_limit_does_not_bound_the_scan_past_the_mark(tmp_path):
    db_path, ids = _db(
        tmp_path,
        [("Archive", f"a {_url(1)} b"), ("Archive", f"c {_url(2)} d"), ("Archive", f"{_url(3)}")],
    )

    _, fetched = _run(db_path, limit=2)

    assert fetched == [_url(1), _url(2), _url(3)]
    assert _mark(db_path) == ids[2]


def test_limit_still_bounds_a_since_rescan(tmp_path):
    db_path, _ = _db(
        tmp_path,
        [("Archive", f"a {_url(1)} b"), ("Archive", f"c {_url(2)} d"), ("Archive", f"{_url(3)}")],
    )

    _, fetched = _run(db_path, since="2026-09-01", limit=2)

    assert fetched == [_url(1), _url(2)]


def test_the_fetch_cap_is_on_unless_turned_off(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_process_sharepoint", lambda args: seen.update(vars(args)))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    db = str(tmp_path / "x.db")

    monkeypatch.setattr("sys.argv", ["brain", "--db", db, "process-sharepoint", "--limit", "200"])
    cli.main()
    assert seen["max_fetches"] == 100

    monkeypatch.setattr(
        "sys.argv", ["brain", "--db", db, "process-sharepoint", "--max-fetches", "0"]
    )
    cli.main()
    assert seen["max_fetches"] == 0


def test_since_rescans_by_date_and_leaves_the_mark_alone(tmp_path):
    db_path, ids = _db(tmp_path, [("Archive", f"a {_url(1)} b"), ("Archive", f"c {_url(2)} d")])
    _set_mark(db_path, ids[1])

    _, fetched = _run(db_path, since="2026-09-11")

    assert fetched == [_url(2)]
    assert _mark(db_path) == ids[1]


def test_the_nightly_pass_scans_by_the_mark_with_a_fetch_cap():
    line = next(ln for ln in WRAPPER.read_text().splitlines() if "src.cli process-sharepoint" in ln)
    assert "--limit" not in line
    assert "--max-fetches" in line


def test_a_commit_from_another_process_mid_scan_does_not_lock_the_pass(tmp_path):
    """The scan cursor stayed open on the connection every fetch commits on. In
    WAL mode a commit from any other process in between left that connection on
    a stale snapshot, and its next write failed at once with 'database is
    locked', which busy_timeout does not retry: the pass exited 1, the mark was
    not written, and the file already downloaded went unrecorded."""
    db_path, ids = _db(
        tmp_path,
        [("Archive", f"a {_url(1)} b"), ("Archive", f"c {_url(2)} d"), ("Archive", f"e {_url(3)}")],
    )
    fetched = []

    def fake_fetch(url, out_dir):
        if not fetched:
            other = get_connection(str(db_path))
            other.execute(
                "INSERT INTO emails (message_id, date_received, content, mailbox_name) "
                "VALUES ('m-other', '2026-09-30T09:00:00', 'no link', 'Archive')"
            )
            other.commit()
            other.close()
        fetched.append(url)
        return SharepointFetchResult(url=url, status="ok")

    options = {"db": str(db_path), "since": None, "limit": 0, "dry_run": False}
    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link", side_effect=fake_fetch):
        rc = cli.cmd_process_sharepoint(argparse.Namespace(**options))

    assert rc == 0
    assert fetched == [_url(1), _url(2), _url(3)]
    assert _mark(db_path) == ids[-1]

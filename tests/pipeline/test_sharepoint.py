"""`brain process-sharepoint` stage by stage: the retry pass, then the scan, and the modes
that fetch nothing.

sharepoint-cli is a fake that answers each URL with a status; the store is a temporary
one. These read the orchestrator alone (src/pipeline/sharepoint.py).
"""

import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from src import config
from src.export.sharepoint_fetcher import SharepointFetchResult, record_link_in_db
from src.pipeline import EXIT_REAUTH, sharepoint
from src.store.schema import create_database, get_connection

MARK = "sharepoint_scan_last_id"
TEAMS_MARK = "sharepoint_teams_scan_last_id"


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


def _mark(db, key=MARK):
    conn = get_connection(str(db))
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (key,)).fetchone()
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


# --- Teams: the files sent in a message and the links in its text ---------------------------


def _file(n: int) -> str:
    """A file sent in Teams, as its entry names it: the path's spaces unencoded."""
    return f"https://contoso-my.sharepoint.com/personal/ann/Documents/Chat Files/Plan{n}.docx"


def _file_link(n: int) -> str:
    """The same file as the scan records it."""
    return _file(n).replace(" ", "%20")


def _payload(*files: int) -> dict:
    """A chat message's payload sending these files."""
    entries = [{"objectUrl": _file(n), "fileName": f"Plan{n}.docx"} for n in files]
    return {"id": "1760000000000", "properties": {"files": json.dumps(entries)}}


def _teams(db, *messages):
    """Teams messages, in order: each (payload or None, content_html[, is_system]); their ids."""
    conn = get_connection(str(db))
    chat = conn.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at)"
        " VALUES ('19:example@thread.v2', 'group', '2026-09-01')"
    ).lastrowid
    ids = []
    for n, (raw, html, *system) in enumerate(messages):
        ids.append(
            conn.execute(
                "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at,"
                " content_html, is_system, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"{chat}::{1760000000000 + n}",
                    chat,
                    f"2026-10-{1 + n:02d}T08:00:00.0000000Z",
                    html,
                    system[0] if system else 0,
                    None if raw is None else json.dumps(raw),
                ),
            ).lastrowid
        )
    conn.commit()
    conn.close()
    return ids


def _links(db) -> dict:
    conn = get_connection(str(db))
    rows = dict(conn.execute("SELECT url, message_id FROM sharepoint_links").fetchall())
    conn.close()
    return rows


def test_teams_links_are_fetched_after_the_mail_and_recorded_by_their_message(tmp_path):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    (sent,) = _teams(db, (_payload(2), f'<a href="{_url(3)}">notes</a>'))

    code, fetched = _run(db)

    assert code == 0
    assert fetched == [_url(1), _file_link(2), _url(3)]
    links = _links(db)
    assert links[_file_link(2)] == links[_url(3)] == f"teams:{sent}"
    assert _mark(db, TEAMS_MARK) == sent


def test_a_link_in_mail_and_in_teams_is_fetched_once(tmp_path):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    _teams(db, (None, f'<a href="{_url(1)}">same</a>'))

    _, fetched = _run(db)

    assert fetched == [_url(1)]
    assert _links(db)[_url(1)] == "m-0"


def test_the_fetch_cap_is_shared_with_the_mail_scan(tmp_path):
    db, ids = _store(tmp_path, f"see {_url(1)}")
    (sent,) = _teams(db, (_payload(2), ""))

    _, first = _run(db, max_fetches=1)

    assert first == [_url(1)]
    assert _mark(db) == ids[-1]
    assert _mark(db, TEAMS_MARK) is None, "the message waits for the next run"

    _, second = _run(db, max_fetches=1)

    assert second == [_file_link(2)]
    assert _mark(db, TEAMS_MARK) == sent


def test_an_expired_session_stops_before_teams(tmp_path):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    _teams(db, (_payload(2), ""))

    code, fetched = _run(db, status_for=lambda url: "auth-required")

    assert (code, fetched) == (EXIT_REAUTH, [_url(1)])
    assert _mark(db, TEAMS_MARK) is None


def test_the_teams_mark_stops_before_the_message_it_could_not_finish(tmp_path):
    db, _ = _store(tmp_path)
    first, _second = _teams(db, (_payload(1), ""), (_payload(2), ""))

    code, fetched = _run(
        db, status_for=lambda url: "auth-required" if url == _file_link(2) else "ok"
    )

    assert (code, fetched) == (EXIT_REAUTH, [_file_link(1), _file_link(2)])
    assert _mark(db, TEAMS_MARK) == first


def test_the_teams_mark_passes_messages_with_no_link(tmp_path):
    db, _ = _store(tmp_path)
    *_, last = _teams(db, (_payload(1), ""), (None, "<p>no link</p>"), (None, None))

    _, fetched = _run(db)

    assert fetched == [_file_link(1)]
    assert _mark(db, TEAMS_MARK) == last


def test_a_teams_dry_run_counts_and_moves_no_mark(tmp_path, capsys):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    _teams(db, (_payload(2, 3), ""))

    code, fetched = _run(db, dry_run=True)

    assert (code, fetched) == (0, [])
    assert _mark(db) is None and _mark(db, TEAMS_MARK) is None
    out = capsys.readouterr().out
    assert "Teams messages with a SharePoint link: 1" in out
    assert "New URLs: 3" in out


def test_system_messages_are_not_read(tmp_path):
    db, _ = _store(tmp_path)
    _teams(db, (None, f"<recording>{_url(1)}</recording>", 1))

    _, fetched = _run(db)

    assert fetched == []


def test_a_rescan_by_date_reads_teams_from_that_date_and_keeps_the_mark(tmp_path):
    db, _ = _store(tmp_path)
    _teams(db, (_payload(1), ""), (_payload(2), ""))

    _, fetched = _run(db, since="2026-10-02")

    assert fetched == [_file_link(2)]
    assert _mark(db, TEAMS_MARK) is None


# --- --urls-file: the shared-with-me backfill -----------------------------------------------

WORDS = "The plan for next year, in enough words to pass the noise filter easily."


def _urls_file(tmp_path, *lines) -> Path:
    path = tmp_path / "shared.jsonl"
    path.write_text("\n".join(ln if isinstance(ln, str) else json.dumps(ln) for ln in lines))
    return path


def _run_storing(db, **options):
    """_run with a fetch that writes a file with text, so each fetch makes a document."""
    args = {"db": str(db), "since": None, "limit": 0, "dry_run": False, **options}

    def fetch(url, out_dir):
        f = Path(out_dir) / f"{url.rsplit('/', 1)[-1]}.txt"
        f.write_text(f"{WORDS} {url}")
        return SharepointFetchResult(url=url, status="ok", local_path=f, file_name=f.name)

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as fake:
        fake.side_effect = fetch
        code = sharepoint.run(Namespace(**args))
    return code, [call.args[0] for call in fake.call_args_list]


def test_a_urls_file_fetches_each_line_dated_by_it_and_moves_no_mark(tmp_path):
    db, _ = _store(tmp_path, f"see {_url(1)}")
    _teams(db, (_payload(2), ""))
    path = _urls_file(
        tmp_path,
        {"url": _url(3), "date": "2026-05-04T10:00:00Z", "label": "Budget"},
        "",
        {"url": _file(4), "date": "2026-06-01T09:30:00+03:00"},
    )

    code, fetched = _run_storing(db, urls_file=str(path))

    assert code == 0
    assert fetched == [_url(3), _file_link(4)], "the scans and the retry pass do not run"
    assert _mark(db) is None and _mark(db, TEAMS_MARK) is None
    conn = get_connection(str(db))
    rows = conn.execute(
        "SELECT l.url, l.message_id, e.date_received FROM sharepoint_links l"
        " JOIN emails e ON e.message_id = l.document_message_id ORDER BY l.url"
    ).fetchall()
    conn.close()
    assert sorted(tuple(r) for r in rows) == sorted(
        [
            (_url(3), "shared-with-me", "2026-05-04T10:00:00Z"),
            (_file_link(4), "shared-with-me", "2026-06-01T09:30:00+03:00"),
        ]
    )


def test_a_urls_file_keeps_the_fetch_cap_and_skips_what_is_fetched(tmp_path):
    db, _ = _store(tmp_path)
    conn = get_connection(str(db))
    record_link_in_db(conn, url=_url(1), message_id="m-0", status="ok")
    conn.close()
    path = _urls_file(
        tmp_path, *({"url": _url(n), "date": "2026-05-04T10:00:00Z"} for n in (1, 2, 3))
    )

    _, fetched = _run_storing(db, urls_file=str(path), max_fetches=1)

    assert fetched == [_url(2)]


def test_a_urls_file_dry_run_counts_only(tmp_path, monkeypatch, capsys):
    db, _ = _store(tmp_path)
    monkeypatch.delenv("SHAREPOINT_HOST")
    path = _urls_file(tmp_path, {"url": _url(1), "date": "2026-05-04T10:00:00Z"})

    code, fetched = _run_storing(db, urls_file=str(path), dry_run=True)

    assert (code, fetched) == (0, [])
    assert _links(db) == {}
    assert "1 to fetch" in capsys.readouterr().out


def test_a_urls_file_skips_the_lines_it_cannot_read_and_says_which(tmp_path, capsys):
    db, _ = _store(tmp_path)
    path = _urls_file(
        tmp_path,
        "{not json",
        {"date": "2026-05-04T10:00:00Z"},
        {"url": _url(1), "date": "last week"},
        {"url": "https://files.example.com/a.pdf", "date": "2026-05-04T10:00:00Z"},
        {"url": _url(2), "date": "2026-05-04T10:00:00Z"},
    )

    code, fetched = _run_storing(db, urls_file=str(path))

    assert (code, fetched) == (0, [_url(2)])
    out = capsys.readouterr().out
    assert "4 line(s) skipped" in out
    for line in (1, 2, 3, 4):
        assert f"line {line}:" in out


def test_a_urls_file_that_is_not_there_exits_1(tmp_path):
    db, _ = _store(tmp_path)

    code, fetched = _run_storing(db, urls_file=str(tmp_path / "missing.jsonl"))

    assert (code, fetched) == (1, [])


def test_the_retry_pass_dates_a_teams_link_by_its_message(tmp_path):
    db, _ = _store(tmp_path)
    (sent,) = _teams(db, (None, "<p>no link</p>"))
    conn = get_connection(str(db))
    record_link_in_db(conn, url=_url(1), message_id=f"teams:{sent}", status="http-error")
    conn.close()

    _, fetched = _run_storing(db)

    assert fetched == [_url(1)]
    conn = get_connection(str(db))
    row = conn.execute(
        "SELECT l.message_id, e.date_received FROM sharepoint_links l"
        " JOIN emails e ON e.message_id = l.document_message_id"
    ).fetchone()
    conn.close()
    assert tuple(row) == (f"teams:{sent}", "2026-10-01T08:00:00.0000000Z")


def test_the_cli_hands_the_urls_file_to_the_pass(tmp_path, monkeypatch):
    import sys

    from src import cli

    handed = []
    monkeypatch.setattr(sharepoint, "run", lambda args: handed.append(args.urls_file) or 0)
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brain",
            "--db",
            str(tmp_path / "brain.db"),
            "process-sharepoint",
            "--urls-file",
            "a.jsonl",
        ],
    )

    cli.main()

    assert handed == ["a.jsonl"]


def test_an_expired_session_stops_the_urls_file(tmp_path):
    db, _ = _store(tmp_path)
    path = _urls_file(tmp_path, *({"url": _url(n), "date": "2026-05-04T10:00:00Z"} for n in (1, 2)))

    code, fetched = _run(db, status_for=lambda url: "auth-required", urls_file=str(path))

    assert (code, fetched) == (EXIT_REAUTH, [_url(1)])

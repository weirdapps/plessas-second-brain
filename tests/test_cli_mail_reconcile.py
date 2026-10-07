"""`brain mail-reconcile`: the report runs anywhere, the writes on the producer only."""

import json
import subprocess
import sys

import pytest

from src.export import mail_reconcile as mr
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError
from src.store import sql_readonly
from src.store.schema import create_database, get_connection

LISTING = {
    "Archive": [
        {
            "Id": "AAMk-archive-1",
            "ReceivedDateTime": "2026-05-04T09:00:00Z",
            "Subject": "Moved",
            "InternetMessageId": "<one@example.com>",
        },
        {
            "Id": "AAMk-gone",
            "ReceivedDateTime": "2026-05-03T09:00:00Z",
            "Subject": "Lost one",
            "InternetMessageId": "<lost@example.com>",
        },
    ]
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(sql_readonly, "REPLICA_STAMP", tmp_path / "absent-db-pull.stamp")
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name,"
        " internet_message_id) VALUES ('AAMk-inbox-1', '2026-05-04T09:00:00Z', 'Moved',"
        " 'Inbox', '<one@example.com>')"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def listing(monkeypatch):
    calls: list[list[str]] = []

    def fake(args, timeout_sec=60):
        calls.append(list(args))
        folder = args[args.index("--folder") + 1]
        return LISTING.get(folder, [])

    monkeypatch.setattr(mr, "run_outlook_cli", fake)
    return calls


def _run(monkeypatch, db, *argv):
    from src import cli

    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "mail-reconcile", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return 0


ARGS = ("--since", "2026-05-01", "--until", "2026-05-06", "--folders", "Archive")


def test_the_report_runs_on_a_replica(db, listing, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    report = tmp_path / "report.json"

    assert _run(monkeypatch, db, *ARGS, "--json", str(report)) == 0

    out = capsys.readouterr().out
    assert "Archive" in out and "Lost one" in out
    written = json.loads(report.read_text())
    assert [m["Id"] for m in written["missing"]] == ["AAMk-gone"]
    assert written["counts"]["Archive"]["by_rfc822_id"] == 1


@pytest.mark.parametrize("flag", ["--refetch", "--record-aliases"])
def test_the_writes_are_refused_on_a_replica(db, listing, monkeypatch, capsys, flag):
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert _run(monkeypatch, db, *ARGS, flag) == 2

    assert listing == [], "refused before anything is listed"
    assert "BRAIN_ROLE=producer" in capsys.readouterr().err


def test_refetch_stages_the_missing_on_the_producer(db, listing, monkeypatch):
    seen = {}

    def fake_refetch(missing, concurrency=2, limit=0):
        seen.update(ids=[m["Id"] for m in missing], concurrency=concurrency, limit=limit)
        return {"requested": len(missing), "staged": len(missing), "failed": [], "attachments": 0}

    monkeypatch.setattr(mr, "refetch", fake_refetch)

    assert _run(monkeypatch, db, *ARGS, "--refetch", "--limit", "5") == 0

    assert seen == {"ids": ["AAMk-gone"], "concurrency": 2, "limit": 5}


def test_record_aliases_notes_the_copies_on_the_producer(db, listing, monkeypatch):
    assert _run(monkeypatch, db, *ARGS, "--record-aliases") == 0

    conn = get_connection(str(db))
    rows = conn.execute("SELECT message_id FROM email_aliases").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["AAMk-archive-1"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OutlookCliAuthRequired("expired"), 4),
        (OutlookCliError(exit_code=5, stderr="502", retryable=True), 5),
        (subprocess.TimeoutExpired(cmd="outlook-cli", timeout=900), 5),
        (mr.TruncatedListing("cap"), 5),
    ],
)
def test_the_estates_exit_codes(db, monkeypatch, error, code):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(mr, "run_outlook_cli", fail)

    assert _run(monkeypatch, db, *ARGS) == code


def test_a_missing_store_is_an_error(tmp_path, listing, monkeypatch):
    assert _run(monkeypatch, tmp_path / "absent.db", *ARGS) == 1

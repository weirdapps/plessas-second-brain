"""Stored mail set against live Outlook: received times and the April 2026 attachment hole.

Mail of 2026-04-06..05-03 was stored with its documents in about 2% of emails against 17-20%
around it, by the retired Apple Mail exporter, whose rows carry its own number and often no
Message-ID: nothing in the pipeline saw the gap. `brain maint mail-attachments` finds each such
email's live message (by id, Message-ID, or subject and time, never guessing between several),
downloads its files as the hourly export does and registers them through the export's own
registrar. `brain maint mail-times` sets the received times the exporter wrote on a Mac in
another zone. outlook-cli is a fake here; every value is invented.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src import config
from src.export import legacy_mail as lm
from src.export import mail_reconcile, outlook_attachments, outlook_export
from src.export.outlook_cli import OutlookCliAuthRequired
from src.store.schema import create_database
from tests import factories
from tests.brain_cli import brain

APRIL = (datetime(2026, 4, 6, tzinfo=UTC), datetime(2026, 5, 4, tzinfo=UTC))


@pytest.fixture(autouse=True)
def _no_pull_stamp(tmp_path, monkeypatch):
    """Open the store the producer's way on any host (see tests/test_sql_readonly.py)."""
    monkeypatch.setattr(config, "REPLICA_STAMP", tmp_path / "absent-db-pull.stamp")


def _graph(n: int) -> str:
    return f"AAMkAGraphIdOfMessageNumber{n:04d}="


def _live(
    n: int,
    received: str,
    subject: str,
    *,
    folder="Archive",
    imid=None,
    sender=None,
    files=("report.pdf",),
    inline=(),
) -> dict:
    return {
        "Id": _graph(n),
        "InternetMessageId": imid,
        "ReceivedDateTime": received,
        "Subject": subject,
        "From": {"EmailAddress": {"Address": sender or "alice@example.com"}},
        "HasAttachments": bool(files),
        "_folder": folder,
        "_files": list(files),
        "_inline": list(inline),
    }


class Outlook:
    """outlook-cli as the commands call it: list-mail by folder and window, get-mail with its
    attachments, download-attachments writing the files."""

    def __init__(self, messages):
        self.messages = messages
        self.calls: list[list[str]] = []
        self.auth_lost = False

    def __call__(self, args, timeout_sec=60):
        self.calls.append(list(args))
        if self.auth_lost:
            raise OutlookCliAuthRequired("session expired")
        verb = args[0]
        if verb == "list-mail":
            folder = mail_reconcile.canonical_folder(args[args.index("--folder") + 1])
            since = args[args.index("--since") + 1]
            until = args[args.index("--until") + 1]
            return [
                {k: v for k, v in m.items() if not k.startswith("_")}
                for m in self.messages
                if m["_folder"] == folder and since <= m["ReceivedDateTime"] < until
            ]
        message = next(m for m in self.messages if m["Id"] == args[1])
        if verb == "get-mail":
            return {
                **{k: v for k, v in message.items() if not k.startswith("_")},
                "Attachments": [
                    {"@odata.type": lm.FILE_ATTACHMENT, "Name": name, "Size": 10, "IsInline": False}
                    for name in message["_files"]
                ]
                + [
                    {"@odata.type": lm.FILE_ATTACHMENT, "Name": name, "Size": 5, "IsInline": True}
                    for name in message["_inline"]
                ],
            }
        if verb == "download-attachments":
            out = Path(args[args.index("--out") + 1])
            for name in message["_files"] + message["_inline"]:
                (out / name).write_bytes(f"{message['Id']}:{name}".encode())
            return {"saved": []}
        raise AssertionError(f"unexpected call {args}")

    def verbs(self, verb: str) -> list[str]:
        return [c[1] for c in self.calls if c[0] == verb]


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = create_database(str(tmp_path / "brain.db"))
    attachments = tmp_path / "attachments"
    monkeypatch.setattr(lm, "ATTACHMENTS_DIR", attachments)
    monkeypatch.setattr(outlook_export, "ATTACHMENTS_DIR", attachments)
    monkeypatch.setattr(lm, "DATA_ROOT", tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def outlook(monkeypatch):
    fake = Outlook([])
    for module in (mail_reconcile, outlook_export, lm, outlook_attachments):
        if hasattr(module, "run_outlook_cli"):
            monkeypatch.setattr(module, "run_outlook_cli", fake)
    return fake


def _legacy(conn, number: int, received: str, subject: str, **columns) -> int:
    """An email as the Apple Mail exporter stored it: its own number, Archive by default."""
    return factories.email(
        conn,
        message_id=number,
        date_received=received,
        subject=subject,
        mailbox_name=columns.pop("mailbox_name", "Archive"),
        **columns,
    )


def _attachments(conn, email_id):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT message_id, filename, sha256 IS NOT NULL FROM attachments WHERE email_id = ?"
            " ORDER BY filename",
            (email_id,),
        )
    ]


# --- finding the live message ---


def _row(**fields) -> lm.Stored:
    values = {
        "email_id": 1,
        "message_id": "5501",
        "received": "2026-04-08T05:39:28Z",
        "subject": "Monthly figures",
        "sender": "alice@example.com",
        "folder": "Archive",
    }
    return lm.Stored(**{**values, **fields})


def _listing(*messages) -> lm.Listing:
    listing = lm.Listing()
    for m in messages:
        listing.add(lm.Live.of(m, m["_folder"]))
    return listing


def test_a_legacy_email_is_found_by_subject_within_two_minutes():
    listing = _listing(_live(1, "2026-04-08T05:39:28Z", "RE: Monthly figures"))
    assert lm.find(_row(), listing).message.id == _graph(1)
    # Stored as the exporter wrote it, Athens wall time with no zone, before schema v41.
    assert lm.find(_row(received="2026-04-08T08:39:28"), listing).message.id == _graph(1)
    assert lm.find(_row(received="2026-04-08T05:42:00Z"), listing).state == "not_found"


def test_an_id_or_alias_or_message_id_comes_before_the_subject():
    listing = _listing(
        _live(1, "2026-04-08T05:39:28Z", "Monthly figures"),
        _live(2, "2026-04-08T05:39:28Z", "Other", imid="<Abc@Example.com>"),
        _live(3, "2026-04-08T05:39:28Z", "Other"),
    )
    assert lm.find(_row(message_id=_graph(3)), listing).state == "by_id"
    assert lm.find(_row(aliases=(_graph(3),)), listing).message.id == _graph(3)
    found = lm.find(_row(imid="abc@example.com"), listing)
    assert (found.state, found.message.id) == ("by_rfc822_id", _graph(2))


def test_several_matches_are_narrowed_by_sender_and_never_guessed():
    one = _live(1, "2026-04-08T05:39:20Z", "Monthly figures", sender="bob@example.com")
    two = _live(2, "2026-04-08T05:39:30Z", "Monthly figures")
    assert lm.find(_row(), _listing(one, two)).message.id == _graph(2)
    three = _live(3, "2026-04-08T05:39:40Z", "Monthly figures")
    assert lm.find(_row(), _listing(one, two, three)).state == "ambiguous"


def test_the_folder_the_email_records_is_looked_in_first():
    archived = _live(1, "2026-04-08T05:39:28Z", "Monthly figures")
    sent = _live(2, "2026-04-08T05:39:28Z", "Monthly figures", folder="Sent Items")
    assert lm.find(_row(folder="Sent Items"), _listing(archived, sent)).message.id == _graph(2)
    # The two copies of a message sent with a copy to oneself are one message.
    a = _live(3, "2026-04-08T05:39:28Z", "x", imid="<m@example.com>")
    b = _live(4, "2026-04-08T05:39:29Z", "x", imid="<m@example.com>", folder="Sent Items")
    assert lm.find(_row(imid="m@example.com"), _listing(a, b)).message.id == _graph(3)


def test_an_email_with_nothing_to_look_it_up_by_is_counted_apart():
    assert lm.find(_row(subject=""), lm.Listing()).state == "no_key"


def test_graph_ids_that_differ_only_in_case_are_other_messages(store):
    """2,432 pairs of stored message ids differ only in case and are distinct messages."""
    upper = _live(1, "2026-04-08T05:39:28Z", "x")
    lower = dict(upper, Id=_graph(1).lower())
    listing = _listing(upper, lower)
    assert lm.find(_row(message_id=_graph(1).lower()), listing).message.id == _graph(1).lower()
    email_id = factories.email(store, message_id=_graph(1), subject="x")
    held = lm._held_by(store, lm.Live.of(lower, "Archive"), {})
    assert email_id not in held


# --- mail-times ---


def test_a_time_an_hour_off_is_set_to_outlooks_and_only_then(store):
    off = _legacy(store, 7001, "2026-06-02T15:00:15Z", "a", internet_message_id="<a@x>")
    right = _legacy(store, 7002, "2026-06-02T15:10:00Z", "b", internet_message_id="<b@x>")
    drift = _legacy(store, 7003, "2026-06-02T15:20:00Z", "c", internet_message_id="<c@x>")
    unlisted = _legacy(store, 7004, "2026-06-02T15:30:00Z", "d", internet_message_id="<d@x>")
    graph = factories.email(
        store,
        message_id=_graph(9),
        date_received="2026-06-02T15:00:00Z",
        subject="e",
        internet_message_id="<e@x>",
    )
    listing = _listing(
        _live(1, "2026-06-02T14:00:15Z", "a", imid="<a@x>"),
        _live(2, "2026-06-02T15:10:00Z", "b", imid="<b@x>"),
        _live(3, "2026-06-02T15:21:30Z", "c", imid="<c@x>"),
        _live(9, "2026-06-02T13:00:00Z", "e", imid="<e@x>"),
    )
    rows = lm.read_mail(store, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 3, tzinfo=UTC))

    counts, corrections = lm.plan_times(rows, listing)

    assert counts == {
        "checked": 4,
        "stored +1 h from Outlook": 1,
        "agree": 1,
        "differ, not by whole hours (left)": 1,
        "not listed": 1,
    }
    assert corrections == [(off, "2026-06-02T14:00:15Z", "2026-06-02T15:00:15Z")]
    assert lm.apply_times(store, corrections) == 1
    dates = dict(store.execute("SELECT id, date_received FROM emails"))
    assert dates[off] == "2026-06-02T14:00:15Z"
    assert (dates[right], dates[drift], dates[unlisted]) == (
        "2026-06-02T15:10:00Z",
        "2026-06-02T15:20:00Z",
        "2026-06-02T15:30:00Z",
    )
    assert dates[graph] == "2026-06-02T15:00:00Z"  # not the exporter's: never touched
    rows = lm.read_mail(store, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 3, tzinfo=UTC))
    assert lm.plan_times(rows, listing)[1] == []  # a second run finds nothing to set


def test_a_correction_written_meanwhile_is_not_overwritten(store):
    email_id = _legacy(store, 7001, "2026-06-02T15:00:15Z", "a", internet_message_id="<a@x>")
    store.execute(
        "UPDATE emails SET date_received = '2026-06-02T14:00:15Z' WHERE id = ?", (email_id,)
    )
    store.commit()
    assert lm.apply_times(store, [(email_id, "2026-06-02T13:00:15Z", "2026-06-02T15:00:15Z")]) == 0


# --- mail-attachments ---


def _april(store, outlook):
    """Three April emails the exporter stored with no attachment row, and their messages."""
    found = _legacy(store, 5501, "2026-04-08T05:39:28Z", "Monthly figures")
    plain = _legacy(store, 5502, "2026-04-09T07:00:00Z", "No files here")
    twin = _legacy(store, 5503, "2026-04-10T07:00:00Z", "Twin")
    # The same message stored again by the outlook-cli export, under its Graph id.
    twin_copy = factories.email(
        store,
        message_id=_graph(3),
        date_received="2026-04-10T07:00:00Z",
        subject="Twin",
        mailbox_name="Archive",
    )
    factories.attachment(store, email_id=twin_copy, filename="twin.pdf")
    outlook.messages = [
        _live(
            1,
            "2026-04-08T05:39:28Z",
            "Monthly figures",
            files=("figures.xlsx",),
            inline=("image001.png",),
        ),
        _live(2, "2026-04-09T07:00:00Z", "No files here", files=()),
        _live(3, "2026-04-10T07:00:00Z", "Twin"),
    ]
    return found, plain, twin


def test_the_dry_run_counts_per_week_and_writes_nothing(store, outlook, tmp_path):
    found, plain, twin = _april(store, outlook)

    result = lm.backfill_attachments(store, *APRIL, dry_run=True)

    assert outlook.verbs("get-mail") == [_graph(1)]
    assert outlook.verbs("download-attachments") == []
    week = result["weeks"]["2026-04-06"]
    assert (week["checked"], week["matched"], week["with_files"]) == (3, 3, 1)
    assert (week["files"], week["inline"]) == (1, 1)
    assert (week["no_files"], week["held_elsewhere"]) == (1, 1)
    assert _attachments(store, found) == []
    assert store.execute("SELECT COUNT(*) FROM email_aliases").fetchone()[0] == 0
    assert not lm.state_path().exists()


def test_the_files_are_downloaded_and_registered_to_the_email(store, outlook):
    found, plain, twin = _april(store, outlook)

    result = lm.backfill_attachments(store, *APRIL, dry_run=False)

    assert outlook.verbs("download-attachments") == [_graph(1)]
    assert result["totals"]["registered"] == 2
    # Under the email's own message id, with the bytes' hash, as the registrar records them.
    assert _attachments(store, found) == [(5501, "figures.xlsx", 1), (5501, "image001.png", 1)]
    alias = store.execute("SELECT email_id FROM email_aliases WHERE message_id = ?", (_graph(1),))
    assert alias.fetchone()[0] == found
    states = {k: v["state"] for k, v in lm.load_state(lm.state_path()).items()}
    assert states == {str(found): "done", str(plain): "no_files", str(twin): "held_elsewhere"}


def test_a_second_run_downloads_nothing_and_adds_no_row(store, outlook):
    _april(store, outlook)
    lm.backfill_attachments(store, *APRIL, dry_run=False)
    rows = store.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    outlook.calls.clear()

    result = lm.backfill_attachments(store, *APRIL, dry_run=False)

    assert outlook.verbs("download-attachments") == outlook.verbs("get-mail") == []
    assert result["totals"]["checked"] == 0  # settled, or holding attachment rows now
    assert store.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == rows


def test_files_downloaded_before_are_not_registered_twice(store, outlook):
    """A run stopped after the download: the next one registers each file once."""
    found, _, _ = _april(store, outlook)
    lm.backfill_attachments(store, *APRIL, dry_run=False)
    store.execute("DELETE FROM attachments WHERE email_id = ?", (found,))
    lm.state_path().unlink()
    store.commit()
    lm.backfill_attachments(store, *APRIL, dry_run=False)
    lm.backfill_attachments(store, *APRIL, dry_run=False)

    assert len(_attachments(store, found)) == 2


def test_ambiguous_mail_is_left_alone_for_good(store, outlook):
    email_id = _legacy(store, 5501, "2026-04-08T05:39:28Z", "Scanner")
    outlook.messages = [
        _live(1, "2026-04-08T05:39:00Z", "Scanner"),
        _live(2, "2026-04-08T05:39:50Z", "Scanner"),
    ]

    lm.backfill_attachments(store, *APRIL, dry_run=False)
    outlook.calls.clear()
    second = lm.backfill_attachments(store, *APRIL, dry_run=False)

    assert lm.load_state(lm.state_path())[str(email_id)]["state"] == "ambiguous"
    assert outlook.verbs("download-attachments") == [] and second["totals"]["checked"] == 0
    assert _attachments(store, email_id) == []


def test_mail_not_found_is_looked_for_again_a_few_times(store, outlook):
    email_id = _legacy(store, 5501, "2026-04-08T05:39:28Z", "Gone from the folders")
    for _ in range(lm.MAX_ATTEMPTS):
        assert lm.backfill_attachments(store, *APRIL, dry_run=False)["totals"]["checked"] == 1
    record = lm.load_state(lm.state_path())[str(email_id)]
    assert (record["state"], record["attempts"]) == ("not_found", lm.MAX_ATTEMPTS)
    assert lm.backfill_attachments(store, *APRIL, dry_run=False)["totals"]["checked"] == 0


def test_a_login_lost_stops_the_run(store, outlook):
    _april(store, outlook)
    outlook.auth_lost = True

    with pytest.raises(OutlookCliAuthRequired):
        lm.backfill_attachments(store, *APRIL, dry_run=False)


def test_the_command_refuses_to_write_on_a_replica(store, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    code, _, err = brain(
        monkeypatch,
        capsys,
        "--db",
        str(tmp_path / "brain.db"),
        "maint",
        "mail-attachments",
        "--since",
        "2026-04-06",
    )
    assert code == 2 and "replica" in err


def test_the_dry_run_command_prints_a_row_per_week(store, outlook, monkeypatch, capsys, tmp_path):
    _april(store, outlook)
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    code, out, _ = brain(
        monkeypatch,
        capsys,
        "--db",
        str(tmp_path / "brain.db"),
        "maint",
        "mail-attachments",
        "--since",
        "2026-04-06",
        "--until",
        "2026-05-04",
        "--dry-run",
    )
    assert code == 0
    assert "2026-04-06" in out and "with files" in out
    assert not lm.state_path().exists()


def test_the_times_command_dry_run_writes_nothing(store, outlook, monkeypatch, capsys, tmp_path):
    email_id = _legacy(store, 7001, "2026-06-02T15:00:15Z", "a", internet_message_id="<a@x>")
    outlook.messages = [_live(1, "2026-06-02T14:00:15Z", "a", imid="<a@x>")]
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    code, out, _ = brain(
        monkeypatch,
        capsys,
        "--db",
        str(tmp_path / "brain.db"),
        "maint",
        "mail-times",
        "--since",
        "2026-06-01",
        "--until",
        "2026-06-03",
        "--dry-run",
    )
    assert code == 0 and "stored +1 h from Outlook" in out
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    assert (
        conn.execute("SELECT date_received FROM emails WHERE id = ?", (email_id,)).fetchone()[0]
        == "2026-06-02T15:00:15Z"
    )
    conn.close()

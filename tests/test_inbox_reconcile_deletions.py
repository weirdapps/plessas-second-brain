"""Deleted and junked mail is recorded where it is, not relabelled Archive.

The reconcile relabelled every Inbox row missing from the live Inbox as Archive,
wherever the message had gone. On 2026-10-10 the live Deleted Items held 281
messages; 95 of them were in the store, 89 labelled Archive. Each hourly run now
lists Deleted Items and Junk Email as well (one call each), matches the stored
rows by Graph id or RFC822 Message-ID (a move mints a new Graph id), and records
`location` and `deleted_at` in a sync_metadata row per email. Nothing is deleted:
what to forget is the retention policy's decision.
"""

import json
import sys

import pytest

from src.export import inbox_reconcile
from src.store.schema import create_database, get_connection

ROWS = [
    # (message_id, internet_message_id, mailbox_name)
    ("AAMk-kept", "<kept@example.test>", "Inbox"),
    ("AAMk-deleted", "<deleted@example.test>", "Inbox"),
    ("AAMk-junked", "<junked@example.test>", "Inbox"),
    ("AAMk-archived", "<archived@example.test>", "Inbox"),
    ("AAMk-old", "<OLD@example.test>", "Archive"),
]

INBOX = [{"Id": "AAMk-kept", "InternetMessageId": "<kept@example.test>"}]
DELETED = [
    # Moved, so under new Graph ids; the Message-ID is what matches.
    {"Id": "AAMk-new-1", "InternetMessageId": "<deleted@example.test>"},
    {"Id": "AAMk-new-2", "InternetMessageId": "<old@EXAMPLE.test>"},
    {"Id": "AAMk-new-3", "InternetMessageId": "<never-stored@example.test>"},
]
JUNK = [{"Id": "AAMk-new-4", "InternetMessageId": "<junked@example.test>"}]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    for message_id, imid, mailbox in ROWS:
        conn.execute(
            "INSERT INTO emails (message_id, internet_message_id, date_received, subject,"
            " mailbox_name) VALUES (?, ?, '2026-10-05T09:00:00Z', 's', ?)",
            (message_id, imid, mailbox),
        )
    conn.commit()
    conn.close()
    return path


class _Mailbox:
    """The live folders outlook-cli lists, and every listing call made."""

    def __init__(self) -> None:
        self.folders = {"Inbox": INBOX, "DeletedItems": DELETED, "JunkEmail": JUNK}
        self.calls: list[list[str]] = []

    def list_mail(self, args, timeout_sec=60):
        self.calls.append(list(args))
        return list(self.folders[args[args.index("--folder") + 1]])

    def listed(self) -> list[str]:
        return [c[c.index("--folder") + 1] for c in self.calls]


@pytest.fixture
def mailbox(monkeypatch):
    box = _Mailbox()
    monkeypatch.setattr(inbox_reconcile, "run_outlook_cli", box.list_mail)
    return box


def _mailboxes(path):
    conn = get_connection(str(path))
    try:
        return dict(conn.execute("SELECT message_id, mailbox_name FROM emails").fetchall())
    finally:
        conn.close()


def _locations(path):
    conn = get_connection(str(path))
    try:
        rows = conn.execute(
            "SELECT key, value FROM sync_metadata WHERE key GLOB 'mail_location:*'"
        ).fetchall()
    finally:
        conn.close()
    return {key.split(":", 1)[1]: json.loads(value) for key, value in rows}


def test_deleted_and_junked_mail_is_recorded_not_relabelled(db, mailbox):
    result = inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert result["status"] == "ok"
    mailboxes = _mailboxes(db)
    assert mailboxes == {
        "AAMk-kept": "Inbox",
        "AAMk-deleted": "Inbox",
        "AAMk-junked": "Inbox",
        "AAMk-archived": "Archive",  # gone, and not to Deleted Items or Junk
        "AAMk-old": "Archive",
    }
    located = _locations(db)
    assert set(located) == {"AAMk-deleted", "AAMk-junked", "AAMk-old"}
    assert located["AAMk-deleted"]["location"] == "Deleted Items"
    assert located["AAMk-deleted"]["deleted_at"]
    assert located["AAMk-deleted"]["internet_message_id"] == "deleted@example.test"
    assert located["AAMk-old"]["location"] == "Deleted Items", "matched without case or brackets"
    assert located["AAMk-junked"] == {
        "location": "Junk Email",
        "deleted_at": None,
        "internet_message_id": "junked@example.test",
    }
    assert (result["deleted"], result["junk"], result["restored"]) == (2, 1, 0)


def test_the_two_folders_are_listed_by_their_well_known_names(db, mailbox):
    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    folders = mailbox.listed()
    assert sorted(folders) == ["DeletedItems", "Inbox", "JunkEmail"]


def test_no_row_is_deleted(db, mailbox):
    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert len(_mailboxes(db)) == len(ROWS)


def test_a_record_outlives_the_purge_and_keeps_its_first_date(db, mailbox):
    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)
    first = _locations(db)["AAMk-deleted"]["deleted_at"]

    mailbox.folders["DeletedItems"] = []  # purged from Deleted Items
    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert _mailboxes(db)["AAMk-deleted"] == "Inbox", "a purge is not a move to Archive"
    assert _locations(db)["AAMk-deleted"]["deleted_at"] == first


def test_mail_back_in_the_inbox_is_forgotten_and_stays_inbox(db, mailbox):
    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    # Restored: back in the Inbox under yet another Graph id.
    mailbox.folders["DeletedItems"] = []
    mailbox.folders["Inbox"] = [
        *INBOX,
        {"Id": "AAMk-new-5", "InternetMessageId": "<deleted@example.test>"},
    ]
    result = inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert "AAMk-deleted" not in _locations(db)
    assert _mailboxes(db)["AAMk-deleted"] == "Inbox"
    assert result["restored"] == 1


def test_a_refused_inbox_listing_records_nothing(db, mailbox):
    mailbox.folders["Inbox"] = []

    result = inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert result["status"] == "refused-empty-listing"
    assert _locations(db) == {}
    folders = mailbox.listed()
    assert folders == ["Inbox"], "nothing else is listed for a run that cannot proceed"


def test_main_records_deletions(db, mailbox, monkeypatch):
    monkeypatch.setattr(inbox_reconcile, "is_replica", lambda: False)
    monkeypatch.setattr(sys, "argv", ["inbox_reconcile", "--db", str(db)])

    assert inbox_reconcile.main() == 0

    assert set(_locations(db)) == {"AAMk-deleted", "AAMk-junked", "AAMk-old"}

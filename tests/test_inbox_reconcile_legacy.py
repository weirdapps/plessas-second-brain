"""Mail the retired Apple Mail exporter stored as 'Inbox' with no Message-ID leaves the Inbox.

The reconcile relabels an Inbox row the live Inbox no longer holds, matched by Graph id or by
RFC822 Message-ID. A row the Apple Mail exporter stored without a Message-ID had neither, so it
stayed 'Inbox' for good: 375 rows on 2026-10-21 against 71 messages in the live Inbox, 316 of
them in the live Archive. One received before the oldest message the Inbox holds cannot be in
it, so it takes the reconcile's own label for mail gone from the Inbox, Archive.
"""

import pytest

from src.export import inbox_reconcile
from src.store.schema import create_database, get_connection

INBOX = [
    {
        "Id": "AAMk-kept",
        "InternetMessageId": "<kept@example.test>",
        "ReceivedDateTime": "2026-07-15T16:37:26Z",
    },
]


class _Mailbox:
    def __init__(self) -> None:
        self.folders = {"Inbox": INBOX, "DeletedItems": [], "JunkEmail": []}
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout_sec=60):
        self.calls.append(list(args))
        return list(self.folders[args[args.index("--folder") + 1]])


@pytest.fixture
def mailbox(monkeypatch):
    box = _Mailbox()
    monkeypatch.setattr(inbox_reconcile, "run_outlook_cli", box)
    return box


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    rows = [
        # (message_id, date_received, internet_message_id)
        ("AAMk-kept", "2026-07-15T16:37:26Z", "<kept@example.test>"),
        (4101, "2026-03-02T10:00:00Z", None),  # older than the Inbox's oldest: gone from it
        (4102, "2026-03-02T10:00:00", None),  # the same, stored with no zone (before v41)
        (4103, "2026-07-20T10:00:00Z", None),  # younger: it may still be there
    ]
    for message_id, received, imid in rows:
        conn.execute(
            "INSERT INTO emails (message_id, internet_message_id, date_received, subject,"
            " mailbox_name) VALUES (?, ?, ?, 's', 'Inbox')",
            (message_id, imid, received),
        )
    conn.commit()
    conn.close()
    return path


def _mailboxes(path):
    conn = get_connection(str(path))
    try:
        return {str(k): v for k, v in conn.execute("SELECT message_id, mailbox_name FROM emails")}
    finally:
        conn.close()


def test_a_legacy_row_older_than_the_inbox_is_relabelled(db, mailbox):
    result = inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert _mailboxes(db) == {
        "AAMk-kept": "Inbox",
        "4101": "Archive",
        "4102": "Archive",
        "4103": "Inbox",
    }
    assert result["by_age"] == 2 and result["moved"] == 2


def test_with_no_such_row_the_inbox_is_listed_once(db, mailbox):
    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)
    mailbox.calls.clear()

    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    # Only 4103 is left, younger than the oldest message: one listing for its age.
    inbox = [c for c in mailbox.calls if c[c.index("--folder") + 1] == "Inbox"]
    assert len(inbox) == 2
    conn = get_connection(str(db))
    conn.execute("UPDATE emails SET mailbox_name = 'Archive' WHERE message_id = 4103")
    conn.commit()
    conn.close()
    mailbox.calls.clear()

    inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert [c[c.index("--folder") + 1] for c in mailbox.calls].count("Inbox") == 1


def test_a_times_listing_at_the_cap_relabels_none_by_age(db, mailbox, monkeypatch):
    """A listing that reached --max may have lost its oldest messages (outlook-cli lists the
    newest first), so its oldest time proves nothing about older mail. The Id listing is
    refused at the cap before this; the times listing is a second call, and the Inbox can
    reach the cap between the two."""
    real = mailbox.__call__

    def capped(args, timeout_sec=60):
        if "ReceivedDateTime" in args:
            cap = int(args[args.index("--max") + 1])
            return [{"ReceivedDateTime": "2026-07-15T16:37:26Z"}] * cap
        return real(args, timeout_sec)

    monkeypatch.setattr(inbox_reconcile, "run_outlook_cli", capped)

    result = inbox_reconcile.reconcile_moves(db, max_results=3, track_elsewhere=True)

    assert result["by_age"] == 0
    assert _mailboxes(db)["4101"] == "Inbox"


def test_an_inbox_whose_times_cannot_be_read_relabels_none_by_age(db, mailbox):
    mailbox.folders["Inbox"] = [{"Id": "AAMk-kept", "InternetMessageId": "<kept@example.test>"}]

    result = inbox_reconcile.reconcile_moves(db, track_elsewhere=True)

    assert result["by_age"] == 0
    assert _mailboxes(db)["4101"] == "Inbox"

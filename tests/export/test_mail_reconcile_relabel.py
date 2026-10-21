"""mail-reconcile --relabel: a stored email's folder is one that holds it now.

The owner sends with a copy to himself and clears Sent Items, so his message sits in the store
under the folder of whichever copy it was stored from, while Outlook holds it elsewhere: on
2026-10-20, 14 messages of the last 30 days in the live Inbox and 30 in the live Archive were
stored as 'Sent Items'. A stored email the walk finds, by Graph id, alias or Message-ID, only in
folders other than the one it records takes the first of them; one recorded as deleted keeps
its label, which the Inbox reconcile owns.
"""

from datetime import UTC, datetime

import pytest

from src import config
from src.export import mail_reconcile as mr
from src.store.schema import create_database, get_connection
from tests.export.test_mail_reconcile import FakeOutlook, _msg

SINCE, UNTIL = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_pull_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPLICA_STAMP", tmp_path / "absent-db-pull.stamp")


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    rows = [
        # message_id, mailbox, internet_message_id
        ("AAMk-sent", "Sent Items", "<sent@x>"),  # the Sent copy, cleared: Inbox holds one
        ("AAMk-both", "Sent Items", "<both@x>"),  # still in Sent Items too: stays
        ("AAMk-deleted", "Inbox", "<deleted@x>"),  # recorded deleted: the Inbox reconcile's
        ("AAMk-alias-row", "Inbox", "<alias@x>"),  # moved to Archive under an alias
        ("AAMk-news", "News", None),
    ]
    for message_id, mailbox, imid in rows:
        conn.execute(
            "INSERT INTO emails (message_id, date_received, subject, mailbox_name,"
            " internet_message_id) VALUES (?, '2026-10-10T09:00:00Z', 's', ?, ?)",
            (message_id, mailbox, imid),
        )
    alias_row = conn.execute(
        "SELECT id FROM emails WHERE message_id = 'AAMk-alias-row'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO email_aliases (message_id, email_id, recorded_at)"
        " VALUES ('AAMk-archived', ?, 'now')",
        (alias_row,),
    )
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('mail_location:AAMk-deleted',"
        ' \'{"location": "Deleted Items"}\')'
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def outlook(monkeypatch):
    fake = FakeOutlook(
        {
            "Inbox": [
                _msg("AAMk-inbox-copy", "2026-10-10T09:00:00Z", imid="<sent@x>"),
                _msg("AAMk-inbox-both", "2026-10-10T09:00:00Z", imid="<both@x>"),
            ],
            "Sent Items": [_msg("AAMk-both", "2026-10-10T09:00:00Z", imid="<both@x>")],
            "Archive": [
                _msg("AAMk-archived", "2026-10-10T09:00:00Z", imid="<alias@x>"),
                _msg("AAMk-deleted-copy", "2026-10-10T09:00:00Z", imid="<deleted@x>"),
            ],
        }
    )
    monkeypatch.setattr(mr, "run_outlook_cli", fake)
    return fake


def _mailboxes(path):
    conn = get_connection(str(path))
    try:
        return dict(conn.execute("SELECT message_id, mailbox_name FROM emails"))
    finally:
        conn.close()


def test_an_email_held_only_elsewhere_takes_that_folder(store, outlook, tmp_path):
    report = mr.reconcile(store, list(mr.DEFAULT_FOLDERS), SINCE, UNTIL, tmp_path / "none")

    moves = {(frm, to) for _, frm, to in report.relabels}
    assert moves == {("Sent Items", "Inbox"), ("Inbox", "Archive")}

    conn = get_connection(str(store))
    try:
        assert mr.apply_relabels(conn, report.relabels) == 2
        assert mr.apply_relabels(conn, report.relabels) == 0  # a second run changes nothing
    finally:
        conn.close()
    assert _mailboxes(store) == {
        "AAMk-sent": "Inbox",
        "AAMk-both": "Sent Items",
        "AAMk-deleted": "Inbox",
        "AAMk-alias-row": "Archive",
        "AAMk-news": "News",
    }


def test_a_folder_not_walked_says_nothing(store, outlook, tmp_path):
    """Listed only in the Inbox, an email recorded in Sent Items may still be there."""
    report = mr.reconcile(store, ["Inbox"], SINCE, UNTIL, tmp_path / "none")

    assert report.relabels == []

"""reconcile_moves relabels by ABSENCE, so it must refuse an unreadable inbox.

The function marks every DB row labelled 'Inbox' that is missing from a live
outlook-cli listing as moved to Archive. That makes an empty listing and an
empty inbox indistinguishable from identical input, while producing opposite
actions, and the relabel is one-way with no undo. outlook-cli returns exit 0
with an empty list on several non-fault paths (throttling, an expired session
that does not map to exit 4, a wrong folder name).
"""

from unittest.mock import patch

from src.export.inbox_reconcile import reconcile_moves


@patch("src.export.inbox_reconcile.list_current_inbox_ids")
@patch("src.export.inbox_reconcile.get_connection")
def test_refuses_an_empty_listing_without_touching_the_database(mock_conn, mock_list, tmp_path):
    mock_list.return_value = (set(), set())

    result = reconcile_moves(tmp_path / "b.db")

    assert result["moved"] == 0
    assert result["status"] == "refused-empty-listing"
    mock_conn.assert_not_called()


@patch("src.export.inbox_reconcile.list_current_inbox_ids")
@patch("src.export.inbox_reconcile.get_connection")
def test_refuses_a_listing_that_hit_the_cap(mock_conn, mock_list, tmp_path):
    """A truncated listing proves nothing about absence either."""
    mock_list.return_value = ({f"AAMk{i}" for i in range(10)}, set())

    result = reconcile_moves(tmp_path / "b.db", max_results=10)

    assert result["moved"] == 0
    assert result["status"] == "refused-truncated-listing"
    mock_conn.assert_not_called()


@patch("src.export.inbox_reconcile.list_current_inbox_ids")
def test_a_plausible_listing_still_reconciles(mock_list, tmp_path):
    """The guard must not break the normal path."""
    from src.store.schema import create_database

    db = tmp_path / "b.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name) "
        "VALUES ('AAMkSTILLHERE', '2026-09-01T00:00:00Z', 'a', 'Inbox')"
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name) "
        "VALUES ('AAMkGONE', '2026-09-01T00:00:00Z', 'b', 'Inbox')"
    )
    conn.commit()
    conn.close()

    mock_list.return_value = ({"AAMkSTILLHERE"}, set())
    result = reconcile_moves(db, max_results=5000)

    assert result["status"] == "ok"
    assert result["moved"] == 1

    from src.store.schema import get_connection

    conn = get_connection(str(db))
    rows = dict(conn.execute("SELECT message_id, mailbox_name FROM emails").fetchall())
    conn.close()
    assert rows["AAMkGONE"] == "Archive"
    assert rows["AAMkSTILLHERE"] == "Inbox"

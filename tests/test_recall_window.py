"""recall takes since and until, as search does: every bucket keeps the rows dated inside.

recall had no date window: its filter took days alone, the lookback of the dossiers, and
refused since and until, so "what did we say about X in March" took a search per source.
"""

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.store.calendar_query import utc_bound
from src.store.schema import get_connection
from tests import seeded_store
from tests.mcp_listing import registered_tool

BUCKETS = (
    "emails",
    "attachments",
    "conversations",
    "decisions",
    "actions",
    "commitments",
    "inline_images",
    "teams",
    "whatsapp",
    "calendar_events",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def _recall(query: str = "okapi", **filter_):
    return registered_tool("recall").fn(query=query, filter=filter_ or None)


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _rows(out) -> dict[str, list]:
    return {k: out[k] for k in BUCKETS if out.get(k)}


def test_recall_lists_since_and_until_among_its_filter_keys(store):
    # Accepted, not refused by name as a key the tool cannot honour.
    _recall(since=_day(400), until=_day(-1))


def test_a_window_after_every_row_empties_every_bucket(store):
    assert _rows(_recall())
    out = _recall(since=_day(-10))

    assert _rows(out) == {}
    assert out["summary"]["total_hits"] == 0


def test_a_window_around_every_row_changes_nothing(store):
    plain = _rows(_recall())
    wide = _rows(_recall(since=_day(400), until=_day(-30)))

    assert {k: [r.get("ref") for r in v] for k, v in wide.items()} == {
        k: [r.get("ref") for r in v] for k, v in plain.items()
    }


def test_every_row_kept_is_dated_inside_the_window(store):
    since, until = _day(3), _day(1)
    out = _recall(since=since, until=until)

    kept = [r for rows in _rows(out).values() for r in rows]
    assert kept
    low, high = utc_bound(since, upper=False), utc_bound(until, upper=True)
    for row in kept:
        moment = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone(UTC)
        assert low <= moment.strftime("%Y-%m-%dT%H:%M:%S") < high, row
    # And the rows dated outside it went: the seeded items four and more days old.
    assert len(kept) < sum(len(v) for v in _rows(_recall()).values())


def test_an_old_match_behind_a_page_of_newer_ones_is_found(store):
    conn = get_connection(str(store))
    for i in range(100, 130):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary, "
            "sender_address, conversation_id, mailbox_name, source_class) VALUES "
            "(?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 'Okapi okapi', 'okapi', "
            "'x@example.com', ?, 'Inbox', 'mail')",
            (i, i, f"m{i}"),
        )
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary, "
        "sender_address, conversation_id, mailbox_name, source_class) VALUES "
        "(200, 200, '2025-03-10T09:00:00Z', 'Okapi', 'okapi', 'x@example.com', 'old', "
        "'Inbox', 'mail')"
    )
    conn.commit()
    conn.close()

    out = registered_tool("recall").fn(
        query="okapi",
        filter={"since": "2025-03-01", "until": "2025-03-31"},
        limit_per_kind=3,
    )
    assert [r["ref"] for r in out["emails"]] == [200]


def test_the_window_is_named_in_the_summary(store):
    out = _recall(since="2025-03-01", until="2025-03-31")

    assert out["summary"]["window"] == {"since": "2025-03-01", "until": "2025-03-31"}


@pytest.mark.parametrize("bad", ["01/03/2025", "2025-3-1", "yesterday"])
def test_a_date_sqlite_cannot_read_is_refused(store, bad):
    with pytest.raises(ToolError):
        _recall(since=bad)

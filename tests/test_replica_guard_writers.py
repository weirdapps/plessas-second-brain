"""The writers outside src.cli refuse a replica too.

#81 guarded the src.cli subcommands and the action lifecycle. The MCP SharePoint
refetch, the people dedup and the Inbox reconcile still wrote to a pulled copy,
which the next pull replaces, the failure behind the 2026-08-29 corruption.
"""

import sys

import pytest


@pytest.fixture
def replica(monkeypatch):
    monkeypatch.setenv("BRAIN_ROLE", "replica")


def test_the_mcp_refetch_refuses_a_replica(replica, monkeypatch):
    from src import mcp_server
    from src.export import sharepoint_fetcher
    from src.store.schema import create_database

    conn = create_database(":memory:")
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)
    monkeypatch.setattr(
        sharepoint_fetcher,
        "fetch_sharepoint_link",
        lambda *a, **k: pytest.fail("fetched on a replica"),
    )

    out = mcp_server.sharepoint_index("refetch", url="https://contoso.sharepoint.com/x")

    assert "Refusing" in out["error"]


def test_the_mcp_listings_still_work_on_a_replica(replica, monkeypatch):
    from src import mcp_server
    from src.store.schema import create_database

    conn = create_database(":memory:")
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    assert mcp_server.sharepoint_index("list_stale") == {"links": []}


def test_the_people_dedup_refuses_a_replica(replica, monkeypatch):
    from src.store import dedup_people

    monkeypatch.setattr(dedup_people, "run_dedup", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(sys, "argv", ["dedup_people", "--dry-run"])

    assert dedup_people.main() == 2


def test_the_inbox_reconcile_refuses_a_replica(replica, monkeypatch):
    from src.export import inbox_reconcile

    monkeypatch.setattr(inbox_reconcile, "reconcile_moves", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(sys, "argv", ["inbox_reconcile"])

    assert inbox_reconcile.main() == inbox_reconcile.REFUSED_ON_REPLICA

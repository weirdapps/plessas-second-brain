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


def _script(name):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"guard_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_topic_dedup_refuses_a_replica(replica, monkeypatch):
    from src.store import dedup_topics

    monkeypatch.setattr(dedup_topics, "run_topic_dedup", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(sys, "argv", ["dedup_topics", "--dry-run"])

    assert dedup_topics.main() == 2


def test_fuzzy_people_applies_nothing_on_a_replica(replica, monkeypatch):
    from src.store import fuzzy_people

    monkeypatch.setattr(fuzzy_people, "get_connection", lambda *a, **k: pytest.fail("opened"))
    monkeypatch.setattr(sys, "argv", ["fuzzy_people", "--apply", "/tmp/reviewed.jsonl"])

    assert fuzzy_people._cli() == 2


def test_recovering_extractions_refuses_a_replica(replica, monkeypatch):
    module = _script("recover_missing_extractions")
    monkeypatch.setattr(module, "get_connection", lambda *a, **k: pytest.fail("opened"))

    assert module.main() == 2


def test_the_secret_scrub_applies_nothing_on_a_replica(replica, monkeypatch):
    module = _script("scrub_secrets")
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: pytest.fail("opened"))

    assert module.main(["--apply"]) == 3


def test_reaping_attachments_applies_nothing_on_a_replica(replica, monkeypatch):
    module = _script("reap_orphan_attachments")
    monkeypatch.setattr(module, "reap_orphan_attachments", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(sys, "argv", ["reap_orphan_attachments", "--apply"])

    assert module.main() == 2

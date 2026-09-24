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


# --- the other side: the producer runs them, and a replica keeps its dry runs.
# conftest runs the session as BRAIN_ROLE=producer.


class _Conn:
    def close(self):
        pass


def test_the_producer_runs_the_dedups(monkeypatch):
    from src.store import dedup_people, dedup_topics

    ran = []
    monkeypatch.setattr(dedup_people, "run_dedup", lambda *a, **k: ran.append("people"))
    monkeypatch.setattr(dedup_topics, "run_topic_dedup", lambda *a, **k: ran.append("topics"))
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])

    assert dedup_people.main() == 0
    assert dedup_topics.main() == 0
    assert ran == ["people", "topics"]


def test_the_producer_applies_people_merges(monkeypatch):
    from src.store import fuzzy_people

    monkeypatch.setattr(fuzzy_people, "get_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(fuzzy_people, "read_review_file", lambda path: [])
    monkeypatch.setattr(fuzzy_people, "apply_reviewed_merges", lambda conn, rows: 0)
    monkeypatch.setattr(sys, "argv", ["fuzzy_people", "--apply", "/tmp/reviewed.jsonl"])

    assert fuzzy_people._cli() == 0


def test_a_replica_still_generates_people_candidates(replica, monkeypatch, tmp_path):
    from src.store import fuzzy_people

    monkeypatch.setattr(fuzzy_people, "get_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(fuzzy_people, "find_fuzzy_candidates", lambda conn, threshold: [])
    monkeypatch.setattr(fuzzy_people, "write_review_file", lambda cands, out: 0)
    monkeypatch.setattr(sys, "argv", ["fuzzy_people", "--generate", "--out", str(tmp_path / "r")])

    assert fuzzy_people._cli() == 0


def test_the_producer_reconciles_the_inbox(monkeypatch, tmp_path):
    from src.export import inbox_reconcile

    db = tmp_path / "brain.db"
    db.touch()
    result = {"scanned_inbox": 0, "moved": 0, "by_outlook_id": 0, "by_internet_id": 0}
    monkeypatch.setattr(inbox_reconcile, "reconcile_moves", lambda *a, **k: result)
    monkeypatch.setattr(sys, "argv", ["inbox_reconcile", "--db", str(db)])

    assert inbox_reconcile.main() == 0


def test_the_producer_gets_past_the_script_guards(monkeypatch, tmp_path):
    """A missing database is the next check after each guard, so its answer shows
    the guard let the run through."""
    recover = _script("recover_missing_extractions")
    monkeypatch.setattr(recover, "DB", tmp_path / "missing.db")
    assert recover.main() == 1

    scrub = _script("scrub_secrets")
    monkeypatch.setattr(scrub, "DEFAULT_DB", tmp_path / "missing.db")
    assert scrub.main(["--apply"]) == 2

    reap = _script("reap_orphan_attachments")
    seen = []
    monkeypatch.setattr(reap, "reap_orphan_attachments", lambda *a, **k: seen.append(k) or _STATS)
    monkeypatch.setattr(sys, "argv", ["reap_orphan_attachments", "--apply"])
    assert reap.main() == 0
    assert seen[0]["apply"] is True


def test_a_replica_keeps_the_scripts_dry_runs(replica, monkeypatch, tmp_path):
    from src.store.schema import create_database

    scrub = _script("scrub_secrets")
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    monkeypatch.setattr(scrub, "DEFAULT_DB", db)
    assert scrub.main([]) == 0

    reap = _script("reap_orphan_attachments")
    seen = []
    monkeypatch.setattr(reap, "reap_orphan_attachments", lambda *a, **k: seen.append(k) or _STATS)
    monkeypatch.setattr(sys, "argv", ["reap_orphan_attachments"])
    assert reap.main() == 0
    assert seen[0]["apply"] is False


def test_the_producer_refetches_a_recorded_link(monkeypatch):
    from src import mcp_server
    from src.store.schema import create_database

    conn = create_database(":memory:")
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.sharepoint_index("refetch", url="https://contoso.sharepoint.com/x")

    assert out == {"error": "refetch: url is not present in sharepoint_links"}


_STATS = {
    "scanned": 0,
    "deleted": 0,
    "bytes_freed": 0,
    "adopted": 0,
    "already_ingested": 0,
    "dirs_removed": 0,
}

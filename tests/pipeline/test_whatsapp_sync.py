"""`brain whatsapp-sync` stage by stage: the snapshot it reads, each stage's budget, and
what a broken push returns.

The stages are fakes that record their turn and their arguments, so these read the
orchestrator alone (src/pipeline/whatsapp_sync.py).
"""

import types

import pytest

from src.export.whatsapp_export import SnapshotUnavailable
from src.pipeline import whatsapp_sync
from src.store.schema import create_database


@pytest.fixture
def stages(tmp_path, monkeypatch):
    """(calls in order, what each was handed, the extraction's result to edit, run)."""
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    calls: list[str] = []
    given: dict[str, tuple] = {}
    extracted = {"extracted": 1, "skipped": 0, "failed": 0, "deferred": 0}

    def fake(name, result):
        def stage(conn, *args, **kwargs):
            calls.append(name)
            given[name] = (args, kwargs)
            if isinstance(result, Exception):
                raise result
            return result

        return stage

    def install(ingest_result):
        fakes = {
            "src.export.whatsapp_export.ingest_snapshot": fake("ingest", ingest_result),
            "src.extract.whatsapp_threads.bound_threads": fake(
                "bound", {"threads_created": 1, "threads_updated": 0}
            ),
            "src.extract.whatsapp_pipeline.extract_threads": fake("extract", extracted),
            "src.store.embeddings.build_whatsapp_index": fake("embed", 1),
        }
        for target, stage in fakes.items():
            monkeypatch.setattr(target, stage)

    install({"messages_inserted": 2, "messages_updated": 0, "chats": 1, "skipped": 0})

    def run(**options):
        args = {"db": db, "snapshot": str(tmp_path / "snapshot.db"), "limit": 6}
        return whatsapp_sync.run(types.SimpleNamespace(**{**args, **options}))

    return calls, given, extracted, install, run


def test_each_stage_runs_in_turn_within_its_budget(stages, tmp_path):
    calls, given, _, _, run = stages

    assert run() == 0

    assert calls == ["ingest", "bound", "extract", "embed"]
    assert given["ingest"] == ((tmp_path / "snapshot.db",), {})
    assert given["extract"] == (
        (),
        {"limit": 6, "deadline_s": whatsapp_sync.WHATSAPP_EXTRACT_DEADLINE_S},
    )
    assert given["embed"] == ((), {"limit": whatsapp_sync.WHATSAPP_EMBED_LIMIT})


def test_without_a_snapshot_named_it_reads_the_configured_one(stages, tmp_path, monkeypatch):
    calls, given, _, _, run = stages
    monkeypatch.setattr("src.config.WHATSAPP_SNAPSHOT", tmp_path / "pushed.db")

    assert run(snapshot=None) == 0

    assert given["ingest"] == ((tmp_path / "pushed.db",), {})


def test_a_snapshot_that_did_not_arrive_returns_66_and_goes_no_further(stages):
    calls, _, _, install, run = stages
    install(SnapshotUnavailable("no snapshot at the path"))

    assert run() == 66

    assert calls == ["ingest"]


def test_sessions_that_failed_extraction_do_not_fail_the_run(stages):
    calls, _, extracted, _, run = stages
    extracted.update(extracted=0, failed=2)

    assert run() == 0

    assert calls[-1] == "embed"


def test_a_store_that_is_not_there_exits_1(stages, tmp_path):
    calls, _, _, _, run = stages

    with pytest.raises(SystemExit) as exc:
        run(db=tmp_path / "absent.db")

    assert exc.value.code == 1
    assert calls == []

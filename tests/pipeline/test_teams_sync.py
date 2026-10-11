"""`brain teams-sync` stage by stage: each stage's budget, and what a dead pull returns.

The stages are fakes that record their turn and their arguments, so these read the
orchestrator alone (src/pipeline/teams_sync.py).
"""

import types

import pytest

from src.pipeline import teams_sync
from src.store.schema import create_database


@pytest.fixture
def stages(tmp_path, monkeypatch):
    """(calls in order, what each was handed, the pull's result to edit, run)."""
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    calls: list[str] = []
    given: dict[str, dict] = {}
    pull = {"chats_pulled": 3, "messages_inserted": 5, "errors": 0}

    def fake(name, result):
        def stage(conn, **kwargs):
            calls.append(name)
            given[name] = kwargs
            return result

        return stage

    fakes = {
        "src.export.teams_export.discover_chats": fake(
            "discover", {"chats_inserted": 1, "chats_updated": 0, "chats_discovered": 3}
        ),
        "src.export.teams_export.pull_messages": fake("pull", pull),
        "src.extract.teams_threads.bound_threads": fake(
            "bound", {"threads_created": 1, "threads_updated": 2}
        ),
        "src.extract.teams_mri.resolve_mris": fake(
            "resolve", {"resolved": 0, "permanent_fail": 0, "retryable_fail": 0}
        ),
        "src.extract.teams_pipeline.extract_threads": fake(
            "extract", {"extracted": 1, "skipped": 0, "failed": 0}
        ),
        "src.store.embeddings.build_teams_index": fake("embed", 1),
    }
    for target, stage in fakes.items():
        monkeypatch.setattr(target, stage)

    def run(**options):
        args = {"db": db, "skip_pull": False, "concurrency": 3, "workers": 5, "limit": 7}
        return teams_sync.run(types.SimpleNamespace(**{**args, **options}))

    return calls, given, pull, run


def test_each_stage_runs_in_turn_within_its_budget(stages):
    calls, given, _, run = stages

    assert run() == 0

    assert calls == ["discover", "pull", "bound", "resolve", "extract", "embed"]
    assert given["discover"] == {"scope": "all"}
    assert given["pull"] == {"concurrency": 3, "deadline_s": teams_sync.TEAMS_PULL_DEADLINE_S}
    assert given["resolve"] == {"max_per_run": 50}
    assert given["extract"] == {
        "workers": 5,
        "limit": 7,
        "deadline_s": teams_sync.TEAMS_EXTRACT_DEADLINE_S,
    }
    assert given["embed"] == {"limit": teams_sync.TEAMS_EMBED_LIMIT}


def test_skip_pull_works_on_what_is_already_stored(stages):
    calls, _, _, run = stages

    assert run(skip_pull=True) == 0

    assert calls == ["bound", "resolve", "extract", "embed"]


def test_a_pull_that_reached_no_chat_returns_5_after_the_later_stages(stages):
    calls, _, pull, run = stages
    pull.update(chats_pulled=0, messages_inserted=0, errors=4)

    assert run() == 5

    assert calls[-1] == "embed"


def test_a_store_that_is_not_there_exits_1(stages, tmp_path):
    calls, _, _, run = stages

    with pytest.raises(SystemExit) as exc:
        run(db=tmp_path / "absent.db")

    assert exc.value.code == 1
    assert calls == []

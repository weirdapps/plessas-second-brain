"""teams-sync reports a dead pull through its exit code (tests-ci-1, cli-3).

pull_messages swallows every non-auth error per chat and counts it, so a
service-wide 403, 429 or 5xx came back as '0 chats; N errors' and the command
still returned None. main() exited 0, sb-teams-sync.sh wrote 'ok' and reset its
failure counter, and Teams ingestion could be dead for most of a day with every
unit green.
"""

import pytest

from src import cli
from src.store.schema import create_database


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """Every step of teams-sync replaced, so only the command's own glue runs."""
    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()

    calls: list[str] = []
    pull = {"chats_pulled": 0, "messages_inserted": 0, "errors": 0}
    extract = {"extracted": 0, "skipped": 0, "failed": 0}

    def _discover(conn, scope):
        calls.append("discover")
        return {"chats_inserted": 0, "chats_updated": 0, "chats_discovered": 3}

    def _pull(conn, concurrency, deadline_s):
        calls.append("pull")
        return dict(pull)

    def _bound(conn):
        calls.append("bound")
        return {"threads_created": 0, "threads_updated": 0}

    def _resolve(conn, max_per_run):
        calls.append("resolve")
        return {"resolved": 0, "permanent_fail": 0, "retryable_fail": 0}

    def _extract(conn, workers, limit, deadline_s):
        calls.append("extract")
        return dict(extract)

    def _embed(conn, limit):
        calls.append("embed")
        return 0

    monkeypatch.setattr("src.export.teams_export.discover_chats", _discover)
    monkeypatch.setattr("src.export.teams_export.pull_messages", _pull)
    monkeypatch.setattr("src.extract.teams_threads.bound_threads", _bound)
    monkeypatch.setattr("src.extract.teams_mri.resolve_mris", _resolve)
    monkeypatch.setattr("src.extract.teams_pipeline.extract_threads", _extract)
    monkeypatch.setattr("src.store.embeddings.build_teams_index", _embed)

    args = type(
        "Args",
        (),
        {"db": db_path, "skip_pull": False, "concurrency": 2, "workers": 4, "limit": 0},
    )()
    return args, calls, pull, extract


def test_every_chat_failing_returns_upstream_after_the_later_steps(stubbed):
    args, calls, pull, _ = stubbed
    pull.update(chats_pulled=0, errors=3)

    rc = cli.cmd_teams_sync(args)

    assert rc == 5
    # The threads already stored are still bounded, extracted and embedded.
    assert calls == ["discover", "pull", "bound", "resolve", "extract", "embed"]


def test_some_chats_pulled_is_not_a_failed_run(stubbed):
    args, _, pull, _ = stubbed
    pull.update(chats_pulled=2, messages_inserted=5, errors=1)

    assert cli.cmd_teams_sync(args) == 0


def test_a_clean_run_returns_zero(stubbed):
    args, _, pull, _ = stubbed
    pull.update(chats_pulled=3, messages_inserted=5)

    assert cli.cmd_teams_sync(args) == 0


def test_no_chats_to_pull_is_not_a_failure(stubbed):
    """Nothing attempted (every chat disabled, or none known yet) has no errors."""
    args, _, _, _ = stubbed

    assert cli.cmd_teams_sync(args) == 0


def test_a_poison_thread_in_extraction_does_not_fail_the_run(stubbed):
    args, _, pull, extract = stubbed
    pull.update(chats_pulled=3, messages_inserted=5)
    extract.update(extracted=0, failed=1)

    assert cli.cmd_teams_sync(args) == 0


def test_skip_pull_returns_zero(stubbed):
    args, calls, _, _ = stubbed
    args.skip_pull = True

    assert cli.cmd_teams_sync(args) == 0
    assert "pull" not in calls


def test_main_exits_with_the_upstream_code(stubbed, monkeypatch):
    args, _, pull, _ = stubbed
    pull.update(chats_pulled=0, errors=3)
    monkeypatch.setattr("sys.argv", ["brain", "--db", str(args.db), "teams-sync"])
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 5

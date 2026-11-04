"""Per-run metrics (issue 173): a job_runs row per run and per stage, tokens keyed by run id.

Every job a timer starts goes through `brain <command>`; the CLI opens a run, the stages a
job marks close one another, and every Claude call's usage is added to the run and to the
stage it fell in. The usage log names the run too. Every value is invented.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.extract import claude_extract
from src.store import job_runs
from src.store.schema import create_database, get_schema_version, set_schema_version
from tests.brain_cli import brain


def _usage(inp, out, read=0, write=0):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=write,
        )
    )


def _rows(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM job_runs ORDER BY id")]
    db.close()
    return rows


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    return str(path)


def test_v42_creates_job_runs():
    assert CURRENT_SCHEMA_VERSION >= 42


def test_job_runs_table_on_a_new_store(store):
    db = sqlite3.connect(store)
    cols = {r[1] for r in db.execute("PRAGMA table_info(job_runs)")}
    db.close()
    assert {
        "run_id",
        "job",
        "stage",
        "started_at",
        "finished_at",
        "status",
        "exit_code",
        "error",
        "llm_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    } <= cols


def test_a_run_writes_one_row_per_stage_and_one_for_the_run(store):
    with job_runs.record("sync", store) as run:
        job_runs.add_usage(_usage(10, 2).usage)  # before any stage: the run only
        job_runs.mark_stage("extract")
        job_runs.add_usage(_usage(100, 20, read=5, write=7).usage)
        job_runs.add_usage(_usage(50, 10).usage)
        job_runs.mark_stage("load")
        run.exit_code = 0
    rows = _rows(store)
    by_stage = {r["stage"]: r for r in rows}
    assert set(by_stage) == {"run", "extract", "load"}
    assert len({r["run_id"] for r in rows}) == 1
    assert all(r["job"] == "sync" and r["status"] == "ok" for r in rows)
    ext = by_stage["extract"]
    assert (ext["llm_calls"], ext["input_tokens"], ext["output_tokens"]) == (2, 150, 30)
    assert (ext["cache_read_tokens"], ext["cache_write_tokens"]) == (5, 7)
    assert by_stage["load"]["llm_calls"] == 0
    whole = by_stage["run"]
    assert (whole["llm_calls"], whole["input_tokens"], whole["output_tokens"]) == (3, 160, 32)
    assert whole["exit_code"] == 0
    assert all(r["finished_at"] for r in rows)
    assert job_runs.current_run_id() is None


def test_a_failed_run_is_recorded_failed_with_its_error(store):
    with pytest.raises(RuntimeError), job_runs.record("teams-sync", store):
        job_runs.mark_stage("pull")
        raise RuntimeError("upstream misbehaved")
    rows = _rows(store)
    assert {r["status"] for r in rows} == {"failed"}
    assert "upstream misbehaved" in next(r for r in rows if r["stage"] == "run")["error"]


def test_a_non_zero_exit_code_is_a_failed_run(store):
    with job_runs.record("calendar-sync", store) as run:
        run.exit_code = 3
    (row,) = _rows(store)
    assert (row["status"], row["exit_code"]) == ("failed", 3)


def test_a_system_exit_is_recorded_with_its_code(store):
    with pytest.raises(SystemExit), job_runs.record("sync", store):
        raise SystemExit(4)
    (row,) = _rows(store)
    assert (row["status"], row["exit_code"]) == ("failed", 4)


def test_a_run_shows_as_running_while_it_runs(store):
    with job_runs.record("sync", store):
        (row,) = _rows(store)
        assert row["status"] == "running" and row["finished_at"] is None


def test_a_store_without_the_table_records_nothing_and_the_job_still_runs(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.commit()
    db.close()
    ran = []
    with job_runs.record("sync", str(path)):
        job_runs.mark_stage("extract")
        ran.append(1)
    assert ran == [1]


def test_a_missing_store_records_nothing(tmp_path):
    with job_runs.record("sync", str(tmp_path / "absent.db")):
        pass
    assert not (tmp_path / "absent.db").exists()


def test_outside_a_run_marks_and_usage_do_nothing():
    job_runs.mark_stage("extract")
    job_runs.add_usage(_usage(1, 1).usage)
    assert job_runs.current_run_id() is None


def test_the_usage_log_names_the_run(store, tmp_path, monkeypatch):
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", str(tmp_path))
    with job_runs.record("sync", store):
        job_runs.mark_stage("extract")
        claude_extract._log_usage(_usage(7, 3), "site.x")
        run_id = job_runs.current_run_id()
    (line,) = (tmp_path).glob("llm-usage-*.jsonl")
    record = json.loads(line.read_text().splitlines()[-1])
    assert record["run_id"] == run_id
    assert next(r for r in _rows(store) if r["stage"] == "extract")["input_tokens"] == 7


def test_should_record_jobs_not_readers_nor_migrate():
    assert job_runs.should_record("sync")
    assert job_runs.should_record("teams-sync")
    assert job_runs.should_record("topic-pages")
    assert not job_runs.should_record("tool")
    assert not job_runs.should_record("migrate")


def test_the_cli_records_a_job_run(store, monkeypatch, capsys):
    from src import cli

    monkeypatch.setattr(cli, "cmd_classify_sources", lambda args: 0)
    code, _, _ = brain(monkeypatch, capsys, "--db", store, "maint", "classify-sources")
    rows = _rows(store)
    assert [r["job"] for r in rows if r["stage"] == "run"] == ["classify-sources"]
    assert rows[0]["exit_code"] == code


def test_migrate_check_reports_a_behind_store(store, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "--db", store, "maint", "migrate", "--check")
    assert code == 0 and "up to date" in out
    db = sqlite3.connect(store)
    db.row_factory = sqlite3.Row
    set_schema_version(db, CURRENT_SCHEMA_VERSION - 1)
    db.close()
    code, out, _ = brain(monkeypatch, capsys, "--db", store, "maint", "migrate", "--check")
    assert code == 3
    assert f"{CURRENT_SCHEMA_VERSION - 1}" in out and f"{CURRENT_SCHEMA_VERSION}" in out
    db = sqlite3.connect(store)
    db.row_factory = sqlite3.Row
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION - 1  # --check writes nothing
    db.close()


def test_migrate_check_on_no_store_is_not_an_error(tmp_path, monkeypatch, capsys):
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(tmp_path / "absent.db"), "maint", "migrate", "--check"
    )
    assert code == 0 and "no store" in out.lower()

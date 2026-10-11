"""src/evals: where the eval sets and results live, and how they are read and written."""

import json
import sqlite3

import pytest

from src import evals


def test_the_eval_home_follows_brain_data_dir_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path / "data"))

    assert evals.eval_home() == tmp_path / "data" / "evals"


def test_without_brain_data_dir_the_eval_home_is_outside_any_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_DATA_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert evals.eval_home() == tmp_path / ".second-brain" / "evals"


def test_an_explicit_base_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path / "elsewhere"))

    assert evals.eval_home(tmp_path) == tmp_path / "evals"


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


def test_a_read_only_connection_cannot_write(store):
    conn = evals.connect_read_only(store)

    assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM t")


def test_a_replica_is_opened_immutable_and_the_producer_is_not(store, monkeypatch):
    """Nothing writes a replica but the pull, which replaces the file, and plain
    read-only opening has failed there; the producer's store is live, so it keeps
    its locks."""
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    assert "immutable=1" in evals.read_only_uri(store)

    monkeypatch.setenv("BRAIN_ROLE", "producer")
    assert "immutable=1" not in evals.read_only_uri(store)
    assert "mode=ro" in evals.read_only_uri(store)


def test_a_missing_store_is_refused_not_created(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        evals.connect_read_only(tmp_path / "absent.db")

    assert not (tmp_path / "absent.db").exists()


def test_jsonl_round_trip_keeps_greek_readable(tmp_path):
    path = tmp_path / "sub" / "r.jsonl"

    evals.append_jsonl(path, {"q": "προϋπολογισμός"})
    evals.append_jsonl(path, {"q": "budget"})

    assert "προϋπολογισμός" in path.read_text(encoding="utf-8")
    assert evals.read_jsonl(path) == [{"q": "προϋπολογισμός"}, {"q": "budget"}]


def test_a_strict_read_names_the_bad_line_and_a_lenient_one_skips_it(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text('{"a": 1}\nnot json\n\n[1, 2]\n{"b": 2}\n', encoding="utf-8")

    with pytest.raises(ValueError, match=":2"):
        evals.read_jsonl(path)
    assert evals.read_jsonl(path, strict=False) == [{"a": 1}, {"b": 2}]


def test_writing_jsonl_replaces_the_whole_file(tmp_path):
    path = tmp_path / "r.jsonl"
    evals.append_jsonl(path, {"old": True})

    evals.write_jsonl(path, [{"new": 1}, {"new": 2}])

    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"new": 1},
        {"new": 2},
    ]


def mode(path):
    import stat

    return stat.S_IMODE(path.stat().st_mode)


def test_every_directory_made_and_every_file_written_is_private(tmp_path):
    """The sets and results hold mail: owner-only, whatever the umask leaves."""
    appended = tmp_path / "evals" / "retrieval" / "results.jsonl"
    replaced = tmp_path / "evals" / "extraction" / "runs" / "r.jsonl"
    text = tmp_path / "evals" / "extraction" / "cases.md"

    evals.append_jsonl(appended, {"a": 1})
    evals.write_jsonl(replaced, [{"a": 1}])
    evals.write_private(text, "body")

    for directory in (
        tmp_path / "evals",
        tmp_path / "evals" / "retrieval",
        tmp_path / "evals" / "extraction",
        tmp_path / "evals" / "extraction" / "runs",
    ):
        assert mode(directory) == 0o700, directory
    for written in (appended, replaced, text):
        assert mode(written) == 0o600, written


def test_a_stale_temporary_file_does_not_lend_the_set_its_mode(tmp_path):
    path = tmp_path / "cases.jsonl"
    stale = tmp_path / ".cases.jsonl.tmp"
    stale.write_text("old")
    stale.chmod(0o644)

    evals.write_jsonl(path, [{"a": 1}])

    assert mode(path) == 0o600


def test_nothing_is_written_inside_the_repository():
    inside = evals.REPO_ROOT / "evals-should-not-exist" / "results.jsonl"

    with pytest.raises(ValueError, match="repository"):
        evals.append_jsonl(inside, {"a": 1})
    with pytest.raises(ValueError, match="repository"):
        evals.write_jsonl(inside, [])
    with pytest.raises(ValueError, match="repository"):
        evals.write_private(inside, "x")
    assert not inside.parent.exists()
    assert evals.inside_repo(inside)
    assert not evals.inside_repo(evals.eval_home())


def test_a_file_sha_changes_with_its_content(tmp_path):
    path = tmp_path / "set.jsonl"
    path.write_text("one\n")
    first = evals.file_sha(path)
    path.write_text("two\n")

    assert first != evals.file_sha(path)
    assert len(first) == 12

"""A host that holds a pulled copy of the database must not write to it.

On 2026-08-29 a local `embed` on the Mac replica left a WAL that the next pull
replayed over the fresh copy and corrupted it. The pull job has deleted
sidecars ever since, around its rsync, but nothing stopped the write itself.
Write commands now refuse to run on a replica, which the pull job's stamp
marks, and BRAIN_ROLE says otherwise when the stamp is wrong.
"""

import sys

import pytest


@pytest.fixture
def stamp(tmp_path, monkeypatch):
    from src import config

    path = tmp_path / "db-pull.stamp"
    monkeypatch.setattr(config, "REPLICA_STAMP", path)
    monkeypatch.delenv("BRAIN_ROLE", raising=False)
    return path


def test_a_host_the_pull_job_stamped_is_a_replica(stamp):
    from src.config import is_replica

    assert not is_replica()
    stamp.write_text("2026-09-23T22:53:00+00:00")
    assert is_replica()


@pytest.mark.parametrize(
    ("role", "stamped", "expected"),
    [
        ("producer", True, False),
        ("replica", False, True),
        ("PRODUCER", True, False),
    ],
)
def test_brain_role_overrides_the_stamp(stamp, monkeypatch, role, stamped, expected):
    from src.config import is_replica

    if stamped:
        stamp.write_text("x")
    monkeypatch.setenv("BRAIN_ROLE", role)

    assert is_replica() is expected


def _run_cli(monkeypatch, tmp_path, *argv):
    from src import cli

    called = []
    monkeypatch.setattr(cli, "cmd_load", lambda args: called.append("load"))
    monkeypatch.setattr(cli, "cmd_stats", lambda args: called.append("stats"))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(tmp_path / "brain.db"), *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code, called
    return 0, called


def test_a_write_command_is_refused_on_a_replica(stamp, monkeypatch, tmp_path, capsys):
    stamp.write_text("x")

    code, called = _run_cli(monkeypatch, tmp_path, "load")

    assert (code, called) == (2, [])
    assert "BRAIN_ROLE=producer" in capsys.readouterr().err


def test_a_read_command_still_runs_on_a_replica(stamp, monkeypatch, tmp_path):
    stamp.write_text("x")

    code, called = _run_cli(monkeypatch, tmp_path, "stats")

    assert (code, called) == (0, ["stats"])


def test_a_write_command_runs_on_the_producer(stamp, monkeypatch, tmp_path):
    code, called = _run_cli(monkeypatch, tmp_path, "load")

    assert (code, called) == (0, ["load"])


def test_every_read_only_name_is_a_real_command():
    """A misspelt name would leave the real command refused, or let a new one in."""
    import argparse

    from src import cli

    commands = set()
    original = argparse.ArgumentParser.parse_args

    def capture(self, *args, **kwargs):
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                commands.update(action.choices)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        argparse.ArgumentParser.parse_args = original

    assert cli.READ_ONLY_COMMANDS <= commands

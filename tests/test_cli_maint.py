"""`brain maint`: the one-off maintenance commands, apart from the jobs the timers run.

The CLI grew about fifty lines a day of one-off maintenance next to the commands the
timers run. `brain maint NAME` groups them; each alias takes the arguments of the
command it runs, and the old spelling keeps working for one release with one
deprecation line, so docs/DEPLOY.md and colleagues' notes still work as written.
"""

import re
import shlex
from pathlib import Path

import pytest

from src import cli
from tests.brain_cli import brain

REPO = Path(__file__).resolve().parent.parent
ENTRIES = sorted(cli.MAINT_COMMANDS.items())
WHOLE = [name for name, (_, implied, _) in ENTRIES if not implied]


def _old_spelling(name: str) -> list[str]:
    command, implied, _ = cli.MAINT_COMMANDS[name]
    return [command, *(f"--{key.replace('_', '-')}" for key in implied)]


@pytest.fixture
def ran(monkeypatch):
    """Every command function replaced by one that records what it was given."""
    calls: list[tuple[str, dict]] = []

    def recorder(function_name):
        def record(args):
            calls.append((function_name, vars(args)))
            return 0

        return record

    for attribute in dir(cli):
        if attribute.startswith("cmd_"):
            monkeypatch.setattr(cli, attribute, recorder(attribute))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    return calls


def _given(args: dict) -> dict:
    """What a command reads, without how it was spelt."""
    return {
        key: value
        for key, value in args.items()
        if key not in {"command", "maint_command", "_maint_implies"}
    }


def test_maint_lists_every_one_off(monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "maint", "--help")

    assert code == 0
    assert all(name in out for name in [*cli.MAINT_COMMANDS, "scrub"])


def test_maint_alone_shows_the_list_and_fails(monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "maint")

    assert code == 1
    assert "calendar-backfill" in out


@pytest.mark.parametrize(("name", "entry"), ENTRIES, ids=[name for name, _ in ENTRIES])
def test_each_alias_runs_its_command_as_the_old_spelling_does(
    name, entry, ran, monkeypatch, capsys
):
    command, implied, _ = entry

    code_old, _, _ = brain(monkeypatch, capsys, *_old_spelling(name))
    code_new, _, err = brain(monkeypatch, capsys, "maint", name)

    (old_function, old_args), (new_function, new_args) = ran
    assert (code_old, code_new) == (0, 0)
    assert new_function == old_function
    assert _given(new_args) == _given(old_args)
    assert all(new_args[key] == value for key, value in implied.items())
    assert err == ""


def test_an_alias_takes_the_arguments_of_its_command(ran, monkeypatch, capsys):
    brain(monkeypatch, capsys, "maint", "reextract", "--garbled", "--limit", "5")
    brain(monkeypatch, capsys, "maint", "ocr-regarble", "--apply")
    brain(monkeypatch, capsys, "maint", "calendar-backfill", "--dry-run", "--since", "2026-01-01")

    (_, reextract), (_, regarble), (_, backfill) = ran
    assert (reextract["garbled"], reextract["limit"]) == (True, 5)
    assert regarble["apply"] is True
    assert (backfill["backfill"], backfill["dry_run"], backfill["since"]) == (
        True,
        True,
        "2026-01-01",
    )


@pytest.mark.parametrize("name", [name for name, _ in ENTRIES])
def test_the_old_spelling_runs_with_one_deprecation_line(name, ran, monkeypatch, capsys):
    code, _, err = brain(monkeypatch, capsys, *_old_spelling(name))

    assert code == 0
    assert len(ran) == 1
    (line,) = err.splitlines()
    assert "deprecated" in line
    assert shlex.split(line.split("use ", 1)[1])[:3] == ["brain", "maint", name]


@pytest.mark.parametrize(
    "argv",
    [["calendar-sync"], ["process-sharepoint"], ["process-sharepoint", "--max-fetches", "100"]],
)
def test_a_job_run_as_scheduled_implies_no_one_off(argv, ran, monkeypatch, capsys):
    """The aliases share their command's argument objects: a default an alias set on one
    would become the job's own, and the hourly calendar-sync would run a backfill."""
    code, _, err = brain(monkeypatch, capsys, *argv)

    ((_, args),) = ran
    assert (code, err) == (0, "")
    for flag in ("backfill", "ingest_fetched", "refetch_content", "repair_links"):
        assert not args.get(flag)


def _wrapper_commands() -> list[list[str]]:
    """Each `python -m src.cli ...` line the scheduler wrappers run, as words."""
    found = []
    for path in sorted((REPO / "scripts" / "wrappers").glob("*/*.sh")):
        for line in path.read_text().splitlines():
            match = re.search(r"-m src\.cli ([^>|&;]+)", line)
            if match and not line.lstrip().startswith("#"):
                found.append(match.group(1).replace("\\", " ").split())
    return found


def test_no_command_the_timers_run_is_a_one_off():
    commands = _wrapper_commands()
    flags = {
        f"--{key.replace('_', '-')}"
        for _, implied, _ in cli.MAINT_COMMANDS.values()
        for key in implied
    }

    assert {"sync", "calendar-sync", "process-sharepoint"} <= {words[0] for words in commands}
    for words in commands:
        assert words[0] not in WHOLE, words
        assert not flags & set(words), words


@pytest.mark.parametrize("name", WHOLE)
def test_a_replica_refuses_an_alias_as_it_refuses_the_command(name, ran, monkeypatch, capsys):
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code_old, _, _ = brain(monkeypatch, capsys, *_old_spelling(name))
    code_new, _, _ = brain(monkeypatch, capsys, "maint", name)

    expected = 0 if name in cli.READ_ONLY_COMMANDS else 2
    assert (code_old, code_new) == (expected, expected)


# --------------------------------------------------- scrub: scripts/scrub_secrets.py


def test_scrub_hands_its_arguments_to_the_script(monkeypatch, capsys):
    """The script's own parser reads them: --vacuum without --apply is its error."""
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)

    code, _, err = brain(monkeypatch, capsys, "maint", "scrub", "--vacuum")

    assert code == 2
    assert "--vacuum requires --apply" in err


def test_scrub_runs_the_script_over_the_files_it_is_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "plain.txt").write_text("nothing secret here\n")

    code, out, _ = brain(monkeypatch, capsys, "maint", "scrub", "--files", str(tmp_path / "notes"))

    assert code == 0
    assert out


def test_scrub_dry_run_is_allowed_on_a_replica(tmp_path, monkeypatch, capsys):
    """It reads only; its --apply refuses on a replica itself, with exit 3."""
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    (tmp_path / "notes").mkdir()

    dry, _, _ = brain(monkeypatch, capsys, "maint", "scrub", "--files", str(tmp_path / "notes"))
    applied, _, _ = brain(
        monkeypatch, capsys, "maint", "scrub", "--apply", "--files", str(tmp_path / "notes")
    )

    assert (dry, applied) == (0, 3)


def test_scrub_refuses_a_store_named_with_db(tmp_path, monkeypatch, capsys):
    """The script scrubs the configured store only, so --db would name one it ignores."""
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)

    code, _, err = brain(monkeypatch, capsys, "--db", str(tmp_path / "copy.db"), "maint", "scrub")

    assert code == 2
    assert "BRAIN_DATA_DIR" in err

"""A wrapper that leaves work undone on a re-auth sentinel exits 69, never 0.

On 2026-10-20 the gcloud credential was dead for about ten hours and the Outlook and
Teams sessions for longer. Ten wrappers skipped on a sentinel and exited 0: systemd
recorded success, hc-success@ pinged green 62 times for runs that did nothing, 38
briefing slots sent no brief, and the nightly report read "ALL SYSTEMS HEALTHY".
sb-outlook-sync.sh and sb-teams-sync.sh had exited 69 on their own sentinels since
2026-09-19 (tests/test_sync_wrapper_sentinel.py has why 69 and not 75); the rest
followed only now, once RestartPreventExitStatus=69 could go into their retry drop-ins.

Two rules come with the code. Work that does not need the missing credential still
runs: the morning backup, the calendar's facts, the Teams pull, a conversation export,
the attachment stages that read files on disk. And a skip that is not missing work
stays 0: another run holding the lock, an opt-out, a pilot that is off.

Each test runs the real wrapper against a throwaway HOME whose venv python records its
arguments and exits as told.
"""

import subprocess
from pathlib import Path

import pytest

_WRAPPERS = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"

# coreutils timeout is not on every test host; the stub records its bounds and runs
# the command it wraps.
_STUB_TIMEOUT = """#!/bin/bash
echo "timeout $*" >> "$HOME/timeout.log"
while [ "${1#-}" != "$1" ]; do
  case "$1" in -k|-s) shift 2 ;; *) shift ;; esac
done
shift
exec "$@"
"""


def _home(tmp_path: Path, *sentinels: str, body: str = "exit 0\n") -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    (home / ".second-brain").mkdir(parents=True)
    for sentinel in sentinels:
        (home / ".second-brain" / sentinel).touch()
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho "$*" >> "$HOME/calls.log"\n' + body)
    python.chmod(0o755)
    tools = home / "tools"
    tools.mkdir()
    (tools / "timeout").write_text(_STUB_TIMEOUT)
    (tools / "timeout").chmod(0o755)
    return home


def _run(wrapper: str, home: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPERS / wrapper)],
        env={
            "HOME": str(home),
            "PATH": f"{home / 'tools'}:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "SB_CONVERSATION_SYNC_LOCK": str(home / "conversation-sync.lock"),
            "SB_DAILY_SYNC_LOCK": str(home / "daily-sync.lock"),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(home: Path) -> list[str]:
    log = home / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _logs(home: Path) -> str:
    return "".join(p.read_text() for p in sorted((home / ".second-brain" / "logs").glob("*.log")))


@pytest.mark.parametrize(
    ("wrapper", "sentinel"),
    [
        ("sb-calendar-sync.sh", "needs_reauth"),
        ("sb-noon-catchup.sh", "needs_gcloud_reauth"),
        ("sb-topic-pages.sh", "needs_gcloud_reauth"),
    ],
)
def test_a_whole_run_skipped_on_a_sentinel_is_red(tmp_path, wrapper, sentinel):
    """Every stage of these needs the dead session, so nothing runs, and the unit fails."""
    home = _home(tmp_path, sentinel)

    result = _run(wrapper, home)

    assert result.returncode == 69
    assert _calls(home) == []
    logs = _logs(home)
    assert sentinel in logs and "skip" in logs.lower()


def test_the_calendar_keeps_its_facts_while_gcloud_is_down(tmp_path):
    """Times, attendees and cancellations come from Outlook, not the model: meeting prep
    reads them. --skip-extraction leaves each body 'pending' for the next run."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run("sb-calendar-sync.sh", home)

    assert _calls(home) == ["-m src.cli calendar-sync --skip-extraction"]
    assert result.returncode == 69


def test_a_failed_calendar_listing_keeps_its_own_code_while_gcloud_is_down(tmp_path):
    home = _home(tmp_path, "needs_gcloud_reauth", body="exit 4\n")

    assert _run("sb-calendar-sync.sh", home).returncode == 4


def test_the_conversation_sync_exports_while_gcloud_is_down(tmp_path):
    """The export and the session notes read local files; only the extraction calls the
    model, and what the export stages waits for it."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run("sb-conversation-sync.sh", home)

    calls = _calls(home)
    assert any("export-conversations" in c for c in calls)
    assert any("ingest-session-notes" in c for c in calls)
    assert not any("extract-conversations" in c for c in calls)
    assert result.returncode == 69


def test_a_failed_conversation_export_keeps_its_own_code_while_gcloud_is_down(tmp_path):
    home = _home(
        tmp_path,
        "needs_gcloud_reauth",
        body='case "$*" in *export-conversations*) exit 3;; esac\nexit 0\n',
    )

    assert _run("sb-conversation-sync.sh", home).returncode == 3


def test_the_morning_backup_is_taken_while_gcloud_is_down(tmp_path):
    """The backup needs no credential and was skipped with the sync: a gcloud outage at
    07:05 cost the day's snapshot as well as the day's load."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run("sb-daily-sync.sh", home)

    calls = _calls(home)
    assert any("backup_db.py" in c for c in calls)
    assert not any("src.cli sync" in c for c in calls)
    assert not any("action_lifecycle" in c for c in calls)
    assert result.returncode == 69


def test_a_second_daily_run_keeps_the_morning_backup(tmp_path):
    """auth-watch's restore runs the daily sync again once gcloud is back, now that the
    skip is red, and so does the unit's own retry. The snapshot taken at 07:05 stands:
    a second backup cost twelve minutes and replaced the first."""
    home = _home(tmp_path)
    stamp = subprocess.run(["date", "+%Y%m%d"], capture_output=True, text=True).stdout.strip()
    backups = home / "SourceCode" / "plessas-second-brain" / "data" / "backups"
    backups.mkdir(parents=True)
    (backups / f"brain-{stamp}.db").write_text("")

    result = _run("sb-daily-sync.sh", home)

    calls = _calls(home)
    assert not any("backup_db.py" in c for c in calls)
    assert any("src.cli sync" in c for c in calls)
    assert result.returncode == 0
    assert "backup already taken today" in _logs(home)


def test_a_day_without_its_archive_takes_the_backup_again(tmp_path):
    """Where the backup key exists the archive is half the backup: a day with the snapshot
    but not the archive is a backup that failed, and the next run tries again."""
    home = _home(tmp_path)
    stamp = subprocess.run(["date", "+%Y%m%d"], capture_output=True, text=True).stdout.strip()
    backups = home / "SourceCode" / "plessas-second-brain" / "data" / "backups"
    backups.mkdir(parents=True)
    (backups / f"brain-{stamp}.db").write_text("")
    (home / ".second-brain" / "backup.key").write_text("")

    _run("sb-daily-sync.sh", home)

    assert any("backup_db.py" in c for c in _calls(home))


def test_the_attachment_pass_runs_its_local_stages_while_gcloud_is_down(tmp_path):
    """Registration, Phase 1, SharePoint, the reaper, the sweep and the mail reconcile need
    no model. Phase 2 and the image pass do, and wait for the next night."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run("sb-attachment-pass.sh", home)

    calls = " | ".join(_calls(home))
    for stage in (
        "register-attachments",
        "--phase 1",
        "process-sharepoint",
        "reap_orphan_attachments",
        "sweep-files",
        "mail-reconcile",
    ):
        assert stage in calls, stage
    assert "--phase 2" not in calls
    assert "process-images" not in calls
    assert result.returncode == 69


def test_a_failed_stage_still_ends_the_pass_with_65_while_gcloud_is_down(tmp_path):
    home = _home(
        tmp_path,
        "needs_gcloud_reauth",
        body='case "$*" in *register-attachments*) exit 5;; esac\nexit 0\n',
    )

    assert _run("sb-attachment-pass.sh", home).returncode == 65


def test_whatsapp_ingests_while_gcloud_is_down(tmp_path):
    """The snapshot's messages are stored and searchable without the model. --limit 1 tries
    one session, which defers to 'pending' on the dead credential, so the run does not
    spend its slice on calls that cannot succeed."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run("sb-whatsapp-sync.sh", home)

    assert _calls(home) == ["-m src.cli whatsapp-sync --limit 1"]
    assert result.returncode == 69


def test_a_broken_whatsapp_snapshot_keeps_its_own_code_while_gcloud_is_down(tmp_path):
    home = _home(tmp_path, "needs_gcloud_reauth", body="exit 66\n")

    assert _run("sb-whatsapp-sync.sh", home).returncode == 66


def test_teams_pulls_while_gcloud_is_down(tmp_path):
    """Each read takes a chat's newest page only, so a long enough outage loses messages:
    the pull needs the Teams session, not the model, and runs."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run("sb-teams-sync.sh", home)

    calls = _calls(home)
    assert calls[0] == "-m src.cli teams-sync --concurrency 2 --workers 4 --limit 1"
    assert result.returncode == 69


def test_the_hourly_mail_export_runs_and_the_skipped_load_is_red(tmp_path):
    """The exports and the reconcile need Outlook, the load needs the model. The load is
    the main loader since the hourly sync took it over, so skipping it is missing work."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run("sb-outlook-sync.sh", home)

    calls = _calls(home)
    assert sum("src.export.outlook_export" in c for c in calls) == 3
    assert any("src.export.inbox_reconcile" in c for c in calls)
    assert not any("src.cli sync" in c for c in calls)
    assert any("sweep-files" in c for c in calls)
    assert result.returncode == 69


def test_a_failed_mail_export_keeps_its_own_code_while_gcloud_is_down(tmp_path):
    home = _home(
        tmp_path,
        "needs_gcloud_reauth",
        body='case "$*" in *outlook_export*) exit 4;; esac\nexit 0\n',
    )

    assert _run("sb-outlook-sync.sh", home).returncode == 4


def test_the_hourly_load_skipped_for_a_running_sync_stays_green(tmp_path):
    """Another sync holding the lock drains the same staging: not missing work."""
    home = _home(tmp_path)
    tools = home / "tools"
    (tools / "pgrep").write_text("#!/bin/bash\nexit 0\n")
    (tools / "pgrep").chmod(0o755)

    result = _run("sb-outlook-sync.sh", home)

    assert not any("src.cli sync" in c for c in _calls(home))
    assert result.returncode == 0


@pytest.mark.parametrize(
    "wrapper",
    [
        "sb-attachment-pass.sh",
        "sb-calendar-sync.sh",
        "sb-conversation-sync.sh",
        "sb-daily-sync.sh",
        "sb-noon-catchup.sh",
        "sb-outlook-sync.sh",
        "sb-teams-sync.sh",
        "sb-topic-pages.sh",
        "sb-whatsapp-sync.sh",
        "sb-news-sync.sh",
    ],
)
def test_no_sentinel_branch_exits_0(wrapper):
    """The shape of the 2026-10-20 bug, read off the source: a line that names a sentinel
    and is followed, before the branch closes, by `exit 0`."""
    lines = (_WRAPPERS / wrapper).read_text().splitlines()
    for i, line in enumerate(lines):
        names_one = "needs_" in line or "SENTINEL" in line
        if names_one and line.lstrip().startswith(("if ", "elif ", "for ")):
            branch = []
            for following in lines[i + 1 :]:
                if following.strip() in ("fi", "done") or following.lstrip().startswith("elif"):
                    break
                branch.append(following.strip())
            assert "exit 0" not in branch, f"{wrapper}:{i + 1} exits 0 on a sentinel"


def test_a_second_daily_run_still_pings_the_backup_check(tmp_path):
    """brain-backup is an oncalendar check on the 07:05 slot. On 2026-10-24 a restore run
    at 05:34 took the day's snapshot and pinged before that slot; the 07:05 run found
    the snapshot, skipped the backup, sent nothing, and the check went down at 08:10
    with a good snapshot on disk. The skip stands on today's file, so it says so."""
    home = _home(tmp_path)
    (home / "tools" / "curl").write_text('#!/bin/bash\necho "curl $*" >> "$HOME/calls.log"\n')
    (home / "tools" / "curl").chmod(0o755)
    stamp = subprocess.run(["date", "+%Y%m%d"], capture_output=True, text=True).stdout.strip()
    backups = home / "SourceCode" / "plessas-second-brain" / "data" / "backups"
    backups.mkdir(parents=True)
    (backups / f"brain-{stamp}.db").write_text("")

    _run("sb-daily-sync.sh", home, HC_PING_URL="https://hc.invalid/ping/key")

    pings = [c for c in _calls(home) if c.startswith("curl ")]
    assert any(c.endswith("https://hc.invalid/ping/key/brain-backup") for c in pings), pings

"""A sentinel-blocked sync must exit 75 (EX_TEMPFAIL), never 0.

On 2026-09-02 13:19 the M365 session died and `~/.second-brain/needs_reauth`
was set. `sb-outlook-sync.sh` then took its sentinel branch and exited 0 once an
hour for the next 24 hours. systemd recorded SUCCESS every time, fired
`OnSuccess=hc-success@`, and the dead-man's switch stayed green while Inbox,
Archive, Sent, calendar and document curation were all frozen. The mailbox was
a full working day stale before anyone noticed, and what finally surfaced it was
a human asking how fresh the database was, not the monitoring.

`sb-teams-sync.sh` had the identical bug and was fixed on 2026-09-01 (its own
comment records a 20-hour outage hidden behind nine green pings). The fix there
was `exit 75`, and this test pins that convention across both wrappers so the
next one added cannot quietly re-introduce it.

75 is EX_TEMPFAIL: systemd treats it as a failure, so the unit goes red and
OnFailure fires, while the exit code still says "transient, retry later" rather
than "this configuration is broken".
"""

import subprocess
from pathlib import Path

import pytest

_WRAPPERS = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"

# (wrapper filename, sentinel filename the wrapper gates on)
_SENTINEL_GATED = [
    ("sb-outlook-sync.sh", "needs_reauth"),
    ("sb-teams-sync.sh", "needs_teams_reauth"),
]


def _run_with_sentinel(wrapper: Path, sentinel_name: str, home: Path):
    """Run the real wrapper against a throwaway HOME holding only the sentinel.

    Every path the wrapper resolves ($PROJECT, $PYTHON, $LOG, the auth-watch
    self-heal hook) hangs off $HOME, so an empty temp HOME makes the sentinel
    branch the only reachable code path: no network, no database, no Chrome.
    """
    (home / ".second-brain").mkdir(parents=True, exist_ok=True)
    (home / ".second-brain" / sentinel_name).touch()

    return subprocess.run(
        ["/bin/bash", str(wrapper)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(("wrapper_name", "sentinel_name"), _SENTINEL_GATED)
def test_sentinel_skip_exits_tempfail_not_success(wrapper_name, sentinel_name, tmp_path):
    """Exit 0 here is the bug: it tells systemd a skipped sync succeeded."""
    wrapper = _WRAPPERS / wrapper_name
    assert wrapper.is_file(), f"{wrapper_name} missing"

    result = _run_with_sentinel(wrapper, sentinel_name, tmp_path)

    assert result.returncode != 0, (
        f"{wrapper_name} exited 0 while blocked by {sentinel_name}. "
        "systemd records that as SUCCESS and pings the dead-man's switch green, "
        "which is exactly how the 2026-09-02 24-hour mail outage stayed invisible."
    )
    assert result.returncode == 75, (
        f"{wrapper_name} exited {result.returncode}; expected 75 (EX_TEMPFAIL) "
        "to match sb-teams-sync.sh's convention."
    )


@pytest.mark.parametrize(("wrapper_name", "sentinel_name"), _SENTINEL_GATED)
def test_sentinel_skip_is_recorded_in_the_log(wrapper_name, sentinel_name, tmp_path):
    """A red unit is only actionable if the log says which sentinel stopped it."""
    wrapper = _WRAPPERS / wrapper_name
    _run_with_sentinel(wrapper, sentinel_name, tmp_path)

    logs = list((tmp_path / ".second-brain" / "logs").glob("*.log"))
    assert logs, f"{wrapper_name} wrote no log on the sentinel path"
    assert any("skip" in log.read_text().lower() for log in logs), (
        f"{wrapper_name} skipped without saying so in its log"
    )


def test_curate_docs_does_not_gate_on_the_outlook_sentinel(tmp_path):
    """Document curation has no Outlook dependency, so needs_reauth must not stop it.

    `curate_documents_daily.py` reads brain.db and calls Vertex. It contains no
    reference to outlook-cli or sharepoint-cli, and it already has the gate for
    the dependency it does have (needs_gcloud_reauth, checked immediately after).
    The needs_reauth check was copied from the mail wrappers ("mirrors other
    second-brain wrappers") and gated curation on an unrelated subsystem.

    Cost, measured 2026-09-03: the Outlook session died on 09-02 and curation
    skipped for six days' worth of runs while its own SharePoint session was
    healthy the whole time (all probes ok, renewed that morning).
    """
    wrapper = _WRAPPERS / "sb-curate-docs.sh"
    assert wrapper.is_file()

    result = _run_with_sentinel(wrapper, "needs_reauth", tmp_path)

    log = tmp_path / ".second-brain" / "logs" / "curate-docs.log"
    assert log.is_file(), "curate-docs wrote no log"
    assert "needs_reauth sentinel present" not in log.read_text(), (
        "curate-docs still refuses to run because of the Outlook sentinel, "
        "though it never touches Outlook."
    )
    # It should fall through to its real guard instead (no Vertex creds in the
    # temp env), which is a legitimate skip.
    assert result.returncode == 0

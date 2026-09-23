"""A failed *proactive* renewal is not an outage, and must not latch the gate.

`sb-auth-watch.sh` owns `needs_reauth` and `needs_teams_reauth`. Every ingest
wrapper is gated on them, so latching one stops mail, calendar, attachments and
document curation at once. It should therefore latch only when the session is
demonstrably unusable.

It did not. Measured on the VPS on 2026-09-03:

    16:04:45 - outlook bearer expires 2026-09-03T13:45:53.000Z (0h / 2468s remaining)
    16:04:45 - outlook bearer expiring within 1h - attempting silent renew
    16:05:16 - outlook: silent renew failed rc=4 ... AUTH_LOGIN_TIMEOUT
    16:05:16 - outlook silent renew failed - interactive login required
    (needs_reauth created)

`auth-check` had just PASSED and the bearer had 41 minutes left. The only
trigger was the sub-1h renewal window. The renewal failed, so the watchdog shut
down the entire pipeline while holding a working token, and nothing clears the
latch except an interactive login.

The Teams branch latched the same day at 16:05:34 on `failing probes: graph_me`
alone, a single transient probe, with `chatsvcagg` (the audience that actually
carries messages) healthy. A `health-check` run minutes later returned `ok`, so
the condition it latched on had already gone.

These tests stand up stub CLIs on a temporary HOME and assert the latch fires on
"the session is dead" and not on "a renewal I did not need yet did not work".
"""

import subprocess
import time
from pathlib import Path

import pytest

_WATCHER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-auth-watch.sh"


def _bin(home: Path) -> Path:
    d = home / ".local" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _outlook_stub(home: Path, *, seconds_remaining: int, renew_ok: bool) -> None:
    """auth-check reports a live token expiring in N seconds; auth-renew may fail."""
    _write_stub(
        _bin(home) / "outlook-cli",
        f"""
case "$1" in
  auth-check)
    EXP=$(/usr/bin/python3 -c "
import datetime
print((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds={seconds_remaining}))
      .strftime('%Y-%m-%dT%H:%M:%S.000Z'))
")
    echo "{{\\"status\\": \\"ok\\", \\"tokenExpiresAt\\": \\"$EXP\\"}}"
    exit 0 ;;
  auth-renew)
    exit {0 if renew_ok else 1} ;;
esac
exit 0
""",
    )


def _teams_stub(home: Path, *, healthy_after_renew_attempt: bool) -> None:
    """health-check fails on the first call; later calls may recover."""
    _write_stub(
        _bin(home) / "teams-cli",
        f"""
COUNT_FILE="$HOME/.teams-hc-count"
case "$1" in
  health-check)
    N=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
    echo $((N + 1)) > "$COUNT_FILE"
    if [ "$N" -eq 0 ]; then
      echo '{{"overall":"degraded","probes":[{{"name":"graph_me","ok":false}},{{"name":"chatsvcagg_updates","ok":true}}]}}'
      exit 1
    fi
    if [ "{1 if healthy_after_renew_attempt else 0}" = "1" ]; then
      echo '{{"overall":"ok","probes":[{{"name":"graph_me","ok":true}}]}}'
      exit 0
    fi
    echo '{{"overall":"degraded","probes":[{{"name":"graph_me","ok":false}}]}}'
    exit 1 ;;
  auth-renew)
    exit 1 ;;
esac
exit 0
""",
    )


def _run_watcher(home: Path):
    (home / ".second-brain").mkdir(parents=True, exist_ok=True)
    # Neutralise the gcloud probe: it is a separate surface with its own sentinel.
    _write_stub(_bin(home) / "gcloud", "exit 0\n")
    return subprocess.run(
        ["/bin/bash", str(_WATCHER)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash", "UID": "501"},
        capture_output=True,
        text=True,
        timeout=180,
    )


def _log(home: Path) -> str:
    p = home / ".second-brain" / "logs" / "auth-watch.log"
    return p.read_text() if p.is_file() else ""


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="probe needs /usr/bin/python3")
def test_live_bearer_is_not_latched_when_only_the_renewal_failed(tmp_path):
    """41 minutes of working token must not shut the pipeline down."""
    _outlook_stub(tmp_path, seconds_remaining=2468, renew_ok=False)
    _teams_stub(tmp_path, healthy_after_renew_attempt=True)

    _run_watcher(tmp_path)

    sentinel = tmp_path / ".second-brain" / "needs_reauth"
    assert not sentinel.exists(), (
        "auth-watch latched needs_reauth while auth-check passed and the bearer "
        "still had 41 minutes left. That gates mail, calendar, attachments and "
        "curation on a token that demonstrably still works.\n\n" + _log(tmp_path)
    )


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="probe needs /usr/bin/python3")
def test_expired_bearer_still_latches(tmp_path):
    """The latch must keep working for the case it exists for."""
    _outlook_stub(tmp_path, seconds_remaining=-600, renew_ok=False)
    _teams_stub(tmp_path, healthy_after_renew_attempt=True)

    _run_watcher(tmp_path)

    sentinel = tmp_path / ".second-brain" / "needs_reauth"
    assert sentinel.exists(), (
        "auth-watch failed to latch on an expired bearer with no working renewal. "
        "That is the condition the sentinel exists for.\n\n" + _log(tmp_path)
    )


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="probe needs /usr/bin/python3")
def test_teams_transient_probe_failure_is_reprobed_before_latching(tmp_path):
    """A blip that has already cleared must not block Teams ingest."""
    _outlook_stub(tmp_path, seconds_remaining=7200, renew_ok=True)
    _teams_stub(tmp_path, healthy_after_renew_attempt=True)

    _run_watcher(tmp_path)

    sentinel = tmp_path / ".second-brain" / "needs_teams_reauth"
    assert not sentinel.exists(), (
        "auth-watch latched needs_teams_reauth after a single transient probe "
        "failure, though health-check passes again by the time it decided.\n\n" + _log(tmp_path)
    )


def _suspect(home: Path) -> Path:
    return home / ".second-brain" / "teams_reauth_suspect"


def _seed_suspect(home: Path, seconds_ago: int) -> int:
    """A previous pass that already found Teams failing, `seconds_ago` seconds back."""
    (home / ".second-brain").mkdir(parents=True, exist_ok=True)
    first = int(time.time()) - seconds_ago
    _suspect(home).write_text(f"{first}\n")
    return first


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="probe needs /usr/bin/python3")
def test_teams_sustained_failure_still_latches(tmp_path):
    """A genuinely dead Teams session must still stop the sync: failing on two
    passes 20+ minutes apart is the definition of sustained."""
    _outlook_stub(tmp_path, seconds_remaining=7200, renew_ok=True)
    _teams_stub(tmp_path, healthy_after_renew_attempt=False)
    _seed_suspect(tmp_path, seconds_ago=1300)

    _run_watcher(tmp_path)

    sentinel = tmp_path / ".second-brain" / "needs_teams_reauth"
    assert sentinel.exists(), (
        "auth-watch failed to latch on a Teams session that stays degraded "
        "across two passes 21 minutes apart and cannot be renewed.\n\n" + _log(tmp_path)
    )
    assert not _suspect(tmp_path).exists(), "the latch supersedes the suspect marker"
    assert "interactive login required" in _log(tmp_path)


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="probe needs /usr/bin/python3")
def test_teams_first_failed_pass_marks_suspect_and_does_not_latch(tmp_path):
    """ONE failing pass is not an outage (2026-09-23). The captured bearer expires on
    the VPS about ten minutes before the producer's next push lands, the VPS renew
    succeeds about 1 time in 87, and the latch then held sb-teams-sync off for 25
    minutes past recovery, until the next pass cleared it. A pass that fails now only
    marks the session suspect; the gate closes only if it is still failing later."""
    _outlook_stub(tmp_path, seconds_remaining=7200, renew_ok=True)
    _teams_stub(tmp_path, healthy_after_renew_attempt=False)

    _run_watcher(tmp_path)

    assert not (tmp_path / ".second-brain" / "needs_teams_reauth").exists(), _log(tmp_path)
    assert _suspect(tmp_path).exists(), _log(tmp_path)
    assert "marking suspect" in _log(tmp_path)
    assert "interactive login required" not in _log(tmp_path)


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="probe needs /usr/bin/python3")
def test_teams_second_failure_inside_twenty_minutes_does_not_latch(tmp_path):
    """Two failures five minutes apart can both sit inside one ten-minute gap."""
    _outlook_stub(tmp_path, seconds_remaining=7200, renew_ok=True)
    _teams_stub(tmp_path, healthy_after_renew_attempt=False)
    first = _seed_suspect(tmp_path, seconds_ago=300)

    _run_watcher(tmp_path)

    assert not (tmp_path / ".second-brain" / "needs_teams_reauth").exists(), _log(tmp_path)
    # The clock runs from the FIRST failure, so a failure every few minutes still
    # latches 20 minutes in rather than never.
    assert _suspect(tmp_path).read_text().strip() == str(first)


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="probe needs /usr/bin/python3")
def test_a_healthy_teams_pass_clears_the_suspect_marker(tmp_path):
    _outlook_stub(tmp_path, seconds_remaining=7200, renew_ok=True)
    _write_stub(
        _bin(tmp_path) / "teams-cli",
        'case "$1" in health-check) echo \'{"overall":"ok","probes":[]}\'; exit 0 ;; esac\nexit 0\n',
    )
    _seed_suspect(tmp_path, seconds_ago=600)

    _run_watcher(tmp_path)

    assert not _suspect(tmp_path).exists(), _log(tmp_path)
    assert not (tmp_path / ".second-brain" / "needs_teams_reauth").exists()

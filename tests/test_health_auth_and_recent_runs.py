"""The nightly report sees the sessions the jobs depend on, and the day behind it.

On 2026-10-20 the Mac could not reach the producer from mid-morning to late afternoon,
the token push delivered nothing, and the Outlook, Teams and gcloud sessions all
lapsed: Teams went 23.3 hours without a good run, nothing was loaded for 9.6 hours.
By 23:51 every session was back, every unit's last run had succeeded, no sentinel was
left, and the report said "ALL SYSTEMS HEALTHY". It judged each job by systemd's last
Result alone and had no row for any session's expiry.

So the report gains two sections. AUTH reads the expiry each session file records
(never a token) and asks gcloud for an access token it throws away. LAST 24 HOURS
reads the user journal: per job, the runs that failed or were skipped on a re-auth
sentinel (exit 69), and the longest stretch without a successful run, against the
widest gap its timer allows. Neither feeds the freshness ping, which stays about the
data as it is now.
"""

import importlib.util
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"
NOW = datetime(2026, 10, 20, 20, 51, tzinfo=UTC)


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The producer's code path wherever the tests run: the journal view is skipped on a Mac.
    module.IS_MACOS = False
    return module


# --- AUTH: Outlook ------------------------------------------------------------------------


def _outlook(tmp_path, expires_at, **extra):
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "capturedAt": "2026-10-20T07:00:00.000Z",
                "bearer": {"token": "SECRET-BEARER", "expiresAt": expires_at},
                "cookies": [{"name": "c", "value": "SECRET-COOKIE"}],
                **extra,
            }
        )
    )
    return path


def test_a_live_outlook_session_is_ok_with_its_time_left(hc, tmp_path):
    r = hc.check_outlook_session(_outlook(tmp_path, "2026-10-21T08:51:00.000Z"), now=NOW)

    assert r["status"] == "OK"
    assert r["remaining"] == timedelta(hours=12)


def test_an_expired_outlook_session_fails(hc, tmp_path):
    r = hc.check_outlook_session(_outlook(tmp_path, "2026-10-20T11:57:00.000Z"), now=NOW)

    assert r["status"] == "FAIL"
    assert "expired" in r["note"]


def test_no_outlook_session_file_is_not_judged(hc, tmp_path):
    assert hc.check_outlook_session(tmp_path / "absent.json", now=NOW)["status"] == "N/A"


@pytest.mark.parametrize("content", ["{not json", json.dumps({"bearer": {}})])
def test_an_outlook_session_without_a_readable_expiry_fails(hc, tmp_path, content):
    path = tmp_path / "session.json"
    path.write_text(content)

    assert hc.check_outlook_session(path, now=NOW)["status"] == "FAIL"


# --- AUTH: Teams --------------------------------------------------------------------------


def _teams(tmp_path, exp_by_aud, name="session.json", nested=True):
    path = tmp_path / name
    tokens = {aud: {"bearerToken": "SECRET", "exp": exp} for aud, exp in exp_by_aud.items()}
    path.write_text(json.dumps({"bearerToken": "SECRET", "tokens": tokens} if nested else tokens))
    return path


def _epoch(moment):
    return int(moment.timestamp())


GRAPH = "https://graph.microsoft.com"
CSA = "https://chatsvcagg.teams.microsoft.com"


def test_teams_is_judged_by_its_shortest_required_audience(hc, tmp_path):
    path = _teams(
        tmp_path,
        {
            GRAPH: _epoch(NOW + timedelta(minutes=40)),
            CSA: _epoch(NOW + timedelta(hours=8)),
            # Not required by teams-cli, and long expired: not what the sync needs.
            "https://outlook.office.com/search": _epoch(NOW - timedelta(days=30)),
        },
    )

    r = hc.check_teams_session(path, now=NOW)

    assert r["status"] == "OK"
    assert r["remaining"] == timedelta(minutes=40)
    assert r["audience"] == GRAPH


def test_a_teams_token_just_expired_is_the_push_between_rounds(hc, tmp_path):
    """The required audience lives about an hour and the Mac's push renews it every 15
    minutes; at 23:50 a Mac asleep since late evening leaves it lapsed by under an hour,
    and the report must not mail about that every such night."""
    path = _teams(tmp_path, {GRAPH: _epoch(NOW - timedelta(minutes=50)), CSA: _epoch(NOW)})

    r = hc.check_teams_session(path, now=NOW)

    assert r["status"] == "OK"
    assert "expired" in r["note"]


def test_a_teams_token_lapsed_for_hours_warns(hc, tmp_path):
    path = _teams(tmp_path, {GRAPH: _epoch(NOW - timedelta(hours=5)), CSA: _epoch(NOW)})

    r = hc.check_teams_session(path, now=NOW)

    assert r["status"] == "WARN"
    assert GRAPH in r["note"]


def test_a_missing_required_audience_fails(hc, tmp_path):
    path = _teams(tmp_path, {GRAPH: _epoch(NOW + timedelta(hours=1))})

    r = hc.check_teams_session(path, now=NOW)

    assert r["status"] == "FAIL"
    assert CSA in r["note"]


def test_teams_falls_back_to_the_multi_token_file(hc, tmp_path):
    """multi-tokens.json is the per-audience map the capture writes; session.json holds
    the same map under `tokens`, and is what teams-cli reads. Either serves."""
    multi = _teams(
        tmp_path,
        {GRAPH: _epoch(NOW + timedelta(hours=1)), CSA: _epoch(NOW + timedelta(hours=2))},
        name="multi-tokens.json",
        nested=False,
    )

    r = hc.check_teams_session(tmp_path / "session.json", now=NOW, multi_path=multi)

    assert r["status"] == "OK"


def test_no_session_value_ever_reaches_the_row(hc, tmp_path):
    outlook = hc.check_outlook_session(_outlook(tmp_path, "2026-10-21T08:51:00Z"), now=NOW)
    teams = hc.check_teams_session(
        _teams(tmp_path, {GRAPH: _epoch(NOW), CSA: _epoch(NOW)}), now=NOW
    )

    assert "SECRET" not in json.dumps([outlook, teams], default=str)


# --- AUTH: gcloud -------------------------------------------------------------------------


def test_gcloud_is_usable_when_it_mints_a_token_nobody_reads(hc, tmp_path):
    seen = {}

    def run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    r = hc.check_gcloud_adc(gcloud="/fake/gcloud", run=run)

    assert r["status"] == "OK"
    assert seen["cmd"] == ["/fake/gcloud", "auth", "application-default", "print-access-token"]
    # The token is printed to stdout: discarded, never captured.
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL


def test_gcloud_that_cannot_mint_a_token_fails(hc):
    r = hc.check_gcloud_adc(
        gcloud="/fake/gcloud", run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )

    assert r["status"] == "FAIL"


def test_gcloud_that_hangs_fails(hc):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    assert hc.check_gcloud_adc(gcloud="/fake/gcloud", run=run)["status"] == "FAIL"


def test_a_host_without_gcloud_is_not_judged(hc, monkeypatch):
    monkeypatch.setattr(hc, "GCLOUD_CANDIDATES", ())
    monkeypatch.setattr(hc.shutil, "which", lambda name: None)

    assert hc.check_gcloud_adc()["status"] == "N/A"


# --- Sentinels: their age ------------------------------------------------------------------


def test_a_sentinel_reports_how_long_it_has_been_up(hc, tmp_path):
    import os
    import time

    sentinel = tmp_path / "needs_gcloud_reauth"
    sentinel.touch()
    three_hours_ago = time.time() - 3 * 3600
    os.utime(sentinel, (three_hours_ago, three_hours_ago))

    r = hc.check_sentinels(state_dir=tmp_path)

    assert r["needs_gcloud_reauth"]["present"] is True
    assert (
        timedelta(hours=2, minutes=59)
        < r["needs_gcloud_reauth"]["age"]
        < timedelta(hours=3, minutes=1)
    )


def test_the_teams_sentinel_is_known_even_when_absent(hc, tmp_path):
    assert hc.check_sentinels(state_dir=tmp_path)["needs_teams_reauth"]["present"] is False


def test_the_report_prints_a_sentinel_age(hc):
    sentinels = {
        "needs_reauth": {
            "present": True,
            "since": "2026-10-20T14:57:00",
            "age": timedelta(hours=6),
            "status": "BLOCKING",
        }
    }

    text, issues = hc.build_report([], {}, {}, sentinels, [])

    assert "needs_reauth: present since 2026-10-20T14:57:00 (6h 0m)" in text
    assert "needs_reauth" in issues


# --- LAST 24 HOURS: the journal -----------------------------------------------------------

DONE = "39f53479d3a045ac8e11786248231fbf"
FAILED = "be02cf6855d2428ba40df7e9d022f03d"
EXITED = "98e322203f7a4ed290d09fe03c09fe15"


def _us(moment):
    return str(int(moment.timestamp() * 1_000_000))


class Journal:
    """Canned `journalctl --user -o json` and `systemctl --user list-units` answers."""

    def __init__(self, timers=("sb-teams-sync", "sb-outlook-sync", "sb-noon-catchup")):
        self.jobs: list[dict] = []
        self.exits: list[dict] = []
        self.timers = timers
        self.calls: list[list[str]] = []
        self._n = 0

    def run(self, unit, end, status=0):
        self._n += 1
        invocation = f"inv{self._n}"
        self.jobs.append(
            {
                "__REALTIME_TIMESTAMP": _us(end),
                "USER_UNIT": f"{unit}.service",
                "JOB_TYPE": "start",
                "JOB_RESULT": "done" if status == 0 else "failed",
                "MESSAGE_ID": DONE if status == 0 else FAILED,
                "USER_INVOCATION_ID": invocation,
            }
        )
        if status:
            self.exits.append(
                {
                    "__REALTIME_TIMESTAMP": _us(end),
                    "USER_UNIT": f"{unit}.service",
                    "MESSAGE_ID": EXITED,
                    "EXIT_CODE": "exited",
                    "EXIT_STATUS": str(status),
                    "USER_INVOCATION_ID": invocation,
                }
            )

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0].endswith("systemctl"):
            out = "".join(f"{t}.timer loaded active waiting x\n" for t in self.timers)
        elif f"MESSAGE_ID={EXITED}" in cmd:
            out = "".join(json.dumps(r) + "\n" for r in self.exits)
        else:
            out = "".join(json.dumps(r) + "\n" for r in self.jobs)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


UNITS = {
    "sb-teams-sync.service": "Teams sync",
    "sb-outlook-sync.service": "Hourly Outlook sync",
    "sb-noon-catchup.service": "Noon catchup",
}


def _hourly(journal, unit, start, end, status=0, minute=30):
    t = start.replace(minute=minute, second=0, microsecond=0)
    while t <= end:
        if t.hour not in (23, 0, 1, 2, 3):  # the producer's night gap, in UTC
            journal.run(unit, t, status)
        t += timedelta(hours=1)


def test_a_clean_day_is_ok(hc):
    journal = Journal()
    _hourly(journal, "sb-teams-sync", NOW - timedelta(hours=40), NOW)
    _hourly(journal, "sb-outlook-sync", NOW - timedelta(hours=40), NOW, minute=10)
    journal.run("sb-noon-catchup", NOW - timedelta(hours=34))
    journal.run("sb-noon-catchup", NOW - timedelta(hours=10))

    r = hc.check_recent_runs(UNITS, now=NOW, run=journal)

    assert r["status"] == "OK"
    assert all(j["status"] == "OK" for j in r["jobs"].values())


def test_the_journal_is_read_for_the_lookback_and_by_message_id(hc):
    journal = Journal()

    hc.check_recent_runs(UNITS, now=NOW, run=journal)

    journal_calls = [c for c in journal.calls if c[0].endswith("journalctl")]
    assert journal_calls and all("--user" in c and "-o" in c for c in journal_calls)
    since = next(a for c in journal_calls for a in c if a.startswith("--since=@"))
    assert int(since.removeprefix("--since=@")) == int((NOW - timedelta(hours=48)).timestamp())


def test_a_long_stretch_without_a_good_run_is_stale_even_after_recovery(hc):
    journal = Journal(timers=("sb-teams-sync",))
    journal.run("sb-teams-sync", NOW - timedelta(hours=25, minutes=18))  # 10-19 19:33
    _hourly(
        journal,
        "sb-teams-sync",
        NOW - timedelta(hours=24),
        NOW - timedelta(hours=2, minutes=5),
        status=69,
    )
    journal.run("sb-teams-sync", NOW - timedelta(hours=1, minutes=58))  # 18:53, recovered

    r = hc.check_recent_runs({"sb-teams-sync.service": "Teams sync"}, now=NOW, run=journal)

    teams = r["jobs"]["sb-teams-sync.service"]
    assert teams["status"] == "STALE"
    assert teams["longest_gap"] > timedelta(hours=23)
    assert teams["skipped"] > 0
    assert r["status"] == "STALE"


def test_runs_skipped_on_a_sentinel_warn_though_the_gaps_fit(hc):
    journal = Journal(timers=("sb-outlook-sync",))
    _hourly(journal, "sb-outlook-sync", NOW - timedelta(hours=40), NOW, minute=10)
    journal.run("sb-outlook-sync", NOW - timedelta(hours=3, minutes=40), status=69)

    r = hc.check_recent_runs(
        {"sb-outlook-sync.service": "Hourly Outlook sync"}, now=NOW, run=journal
    )

    job = r["jobs"]["sb-outlook-sync.service"]
    assert job["status"] == "WARN"
    assert job["skipped"] == 1


def test_a_retried_blip_is_reported_but_not_an_issue(hc):
    journal = Journal(timers=("sb-outlook-sync",))
    _hourly(journal, "sb-outlook-sync", NOW - timedelta(hours=40), NOW, minute=10)
    journal.run("sb-outlook-sync", NOW - timedelta(hours=3, minutes=45), status=75)

    r = hc.check_recent_runs(
        {"sb-outlook-sync.service": "Hourly Outlook sync"}, now=NOW, run=journal
    )

    job = r["jobs"]["sb-outlook-sync.service"]
    assert job["status"] == "OK"
    assert job["failed"] == 1


def test_a_daily_job_missed_once_is_stale(hc):
    journal = Journal(timers=("sb-noon-catchup",))
    journal.run("sb-noon-catchup", NOW - timedelta(hours=34))
    journal.run("sb-noon-catchup", NOW - timedelta(hours=10), status=69)

    r = hc.check_recent_runs({"sb-noon-catchup.service": "Noon catchup"}, now=NOW, run=journal)

    job = r["jobs"]["sb-noon-catchup.service"]
    assert job["status"] == "STALE"
    assert job["longest_gap"] == timedelta(hours=34)


def test_no_good_run_in_the_whole_lookback_is_stale(hc):
    journal = Journal(timers=("sb-noon-catchup",))

    r = hc.check_recent_runs({"sb-noon-catchup.service": "Noon catchup"}, now=NOW, run=journal)

    job = r["jobs"]["sb-noon-catchup.service"]
    assert job["status"] == "STALE"
    assert job["longest_gap"] >= timedelta(hours=48)


def test_a_job_whose_timer_is_off_is_not_held_to_a_gap(hc):
    """The brief pilot disables its own timer when it ends; a stopped timer is a state,
    not a stretch without runs."""
    journal = Journal(timers=())

    r = hc.check_recent_runs({"sb-noon-catchup.service": "Noon catchup"}, now=NOW, run=journal)

    assert r["jobs"]["sb-noon-catchup.service"]["status"] == "OK"


def test_long_running_services_are_left_out(hc):
    journal = Journal(timers=())

    r = hc.check_recent_runs(
        {"sb-mcp.service": "MCP server over HTTP", "sb-noon-catchup.service": "Noon catchup"},
        now=NOW,
        run=journal,
    )

    assert "sb-mcp.service" not in r["jobs"]


def test_an_unreadable_journal_is_a_warning_not_a_pass(hc):
    def run(cmd, **kwargs):
        if cmd[0].endswith("journalctl"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No journal files")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    r = hc.check_recent_runs(UNITS, now=NOW, run=run)

    assert r["status"] == "WARN"
    assert "journal" in r["note"]


def test_the_view_is_not_taken_on_a_mac(hc, monkeypatch):
    monkeypatch.setattr(hc, "IS_MACOS", True)

    assert hc.check_recent_runs(UNITS, now=NOW, run=Journal())["status"] == "N/A"


# --- Acceptance: a 20-Oct-shaped day ------------------------------------------------------


def _twentieth_of_october(journal):
    """UTC times from the audit. Teams: last good run 10-19 19:33, the 22:32 run and its
    three retries failed (75), then a run skipped on its sentinel every hour until the
    restore's good run at 18:53. Outlook: seven runs skipped on its sentinel, 12:12 to
    18:10. The noon catch-up skipped on the gcloud sentinel at 10:17."""
    day = datetime(2026, 10, 20, tzinfo=UTC)
    journal.run("sb-teams-sync", day - timedelta(hours=4, minutes=27))
    for minutes in (32, 39, 46, 53):
        journal.run("sb-teams-sync", day - timedelta(hours=2) + timedelta(minutes=minutes), 75)
    for hour in range(4, 19):
        journal.run("sb-teams-sync", day + timedelta(hours=hour, minutes=30), status=69)
    journal.run("sb-teams-sync", day + timedelta(hours=18, minutes=53))
    for hour in range(4, 21):
        journal.run(
            "sb-outlook-sync",
            day + timedelta(hours=hour, minutes=12),
            status=69 if 12 <= hour <= 18 else 0,
        )
    journal.run("sb-noon-catchup", day - timedelta(hours=13, minutes=43))
    journal.run("sb-noon-catchup", day + timedelta(hours=10, minutes=17), status=69)


def test_a_day_like_the_twentieth_of_october_is_not_healthy(hc):
    journal = Journal()
    _twentieth_of_october(journal)
    # At report time every unit's last run succeeded and no sentinel is left.
    jobs = {u: {"desc": d, "status": "OK"} for u, d in UNITS.items()}
    sentinels = {
        n: {"present": False, "status": "OK"} for n in hc.check_sentinels(Path("/nonexistent"))
    }

    recent = hc.check_recent_runs(UNITS, now=NOW, run=journal)
    text, issues = hc.build_report([], jobs, {}, sentinels, [], recent=recent)

    assert "ALL SYSTEMS HEALTHY" not in text
    # SCHEDULED JOBS reads OK for all three, as it did that night.
    scheduled = text.split("SCHEDULED JOBS", 1)[1].split("LAST 24 HOURS", 1)[0]
    assert "FAIL" not in scheduled
    day = text.split("LAST 24 HOURS", 1)[1]
    teams_line = next(line for line in day.splitlines() if line.strip().startswith("Teams sync"))
    assert "without a good run" in teams_line and "STALE" in teams_line
    assert any("Teams sync" in i for i in issues)
    assert any("Hourly Outlook sync" in i and "sentinel" in i for i in issues)
    assert any("Noon catchup" in i for i in issues)
    # The freshness ping is about the data now, and the data is fresh again.
    ok, _ = hc.freshness_verdict([], sentinels)
    assert ok is True


def test_a_clean_day_reads_healthy(hc):
    journal = Journal()
    _hourly(journal, "sb-teams-sync", NOW - timedelta(hours=40), NOW)
    _hourly(journal, "sb-outlook-sync", NOW - timedelta(hours=40), NOW, minute=10)
    journal.run("sb-noon-catchup", NOW - timedelta(hours=34))
    journal.run("sb-noon-catchup", NOW - timedelta(hours=10))
    jobs = {u: {"desc": d, "status": "OK"} for u, d in UNITS.items()}

    recent = hc.check_recent_runs(UNITS, now=NOW, run=journal)
    text, issues = hc.build_report([], jobs, {}, {}, [], recent=recent)

    assert issues == []
    assert "ALL SYSTEMS HEALTHY" in text


def test_auth_rows_reach_the_report_and_its_issues(hc):
    auth = [
        {"name": "Outlook session", "status": "FAIL", "note": "expired 2026-10-20 11:57 UTC"},
        {"name": "Teams session", "status": "OK", "note": "graph expires in 40m"},
        {"name": "gcloud ADC", "status": "OK", "note": "mints an access token"},
    ]

    text, issues = hc.build_report([], {}, {}, {}, [], auth=auth)

    block = text.split("AUTH", 1)[1]
    assert "Outlook session" in block and "expired 2026-10-20 11:57 UTC" in block
    assert "Outlook session: FAIL" in issues
    assert not any(i.startswith("Teams session") for i in issues)

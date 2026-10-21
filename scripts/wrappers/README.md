# Scheduler wrappers (archive)

The shell wrappers the schedulers actually invoke, kept here so they are
reviewable, diffable and recoverable. Ingestion is driven by these, not by
`src/cli.py` alone: the wrappers own locking, PATH, timeouts, retry and the
liveness stamps the health check reads.

## This is an archive, not a deploy source

**Pulling the repository changes nothing that runs.** The live copies are
`~/.local/bin/*.sh` on each host, and that is what the units and LaunchAgents
execute. A change is made here, reviewed and merged like any other, and then
installed by hand on the host that runs it:

```bash
# on the producer, after pulling a change to a systemd wrapper
install -m 755 scripts/wrappers/systemd/sb-outlook-sync.sh ~/.local/bin/
# on the Mac, after pulling a change to a launchd wrapper
install -m 755 scripts/wrappers/launchd/sb-db-pull.sh ~/.local/bin/
```

Until it is installed the host runs the old copy, and the nightly health check
lists each archived wrapper of its own host's flavour (launchd on a Mac, systemd
elsewhere) whose installed copy differs, under WRAPPER DRIFT.
Verify the installed copy under its real scheduler (below). A fix made on a host
in an emergency goes the other way: copy it back here and commit it, so the
archive never trails what runs. `docs/DEPLOY.md`, section 12, has the rest of an
upgrade.

## Layout

| Directory | Host | Scheduler | Count |
| --- | --- | --- | --- |
| `systemd/` | the producer (a Linux VPS) | `systemctl --user` timers, plus one long-running service | 14 |
| `launchd/` | a Mac that holds a replica | LaunchAgents, and one Claude Code hook | 4, plus one plist template |

What each one runs, and when on the reference deployment (Europe/Athens; the unit
files are host configuration and not in this repository, and `docs/DEPLOY.md`
section 6 shows the same schedule from the pipeline's side):

| Wrapper | Started by | When | What it runs |
| --- | --- | --- | --- |
| `systemd/sb-outlook-sync.sh` | `sb-outlook-sync` | :10 at 01 and 07 to 22 | `sb-auth-watch.sh`, the Inbox, Archive and Sent Items exports, `inbox_reconcile`, `sync` (which loads Claude Code conversations every hour), `sweep-files --policy` |
| `systemd/sb-teams-sync.sh` | `sb-teams-sync` | :30 at 01 and 07 to 22 | `teams-sync` |
| `systemd/sb-whatsapp-sync.sh` | `sb-whatsapp-sync` | :55 at 01 and 07 to 22 | `whatsapp-sync`, after the Mac's :50 push |
| `systemd/sb-calendar-sync.sh` | `sb-calendar-sync` | :48 at 07 to 22 | `calendar-sync` |
| `systemd/sb-news-sync.sh` | `sb-news-sync` | :50 at even hours | `news-sync`, then `sync --lock-wait 600` to load what it staged |
| `systemd/sb-conversation-sync.sh` | `sb-conversation-sync` | 08:45, 14:45, 20:45 | `export-conversations --days 7`, `extract-conversations`, `ingest-session-notes` |
| `systemd/sb-daily-sync.sh` | `sb-daily-sync` | 07:05 | `scripts/backup_db.py`, `sync`, `src.store.action_lifecycle`, and the mail plugin's style-sync when installed |
| `systemd/sb-noon-catchup.sh` | `sb-noon-catchup` | 13:17 | `sync --lock-wait 600` |
| `systemd/sb-attachment-pass.sh` | `sb-attachments` | 02:00 | the nightly attachment pass: registration, Phase 1, Phase 2, images, SharePoint, the orphan reaper, the file sweep, `mail-reconcile --since 7d` |
| `systemd/sb-auth-watch.sh` | `sb-auth-watch`, and `sb-outlook-sync.sh` before each run | every 4 hours | checks the Outlook and Teams sessions, renews them, and sets or clears the re-auth sentinels |
| `systemd/sb-health-check.sh` | `sb-health-check` | 23:50 | `scripts/health_check.py --fix --email-if-issues`, then the `sb-brain-freshness` ping (`docs/DEPLOY.md`, section 7) |
| `systemd/sb-topic-pages.sh` | `sb-topic-pages` | 03:30 | `topic-pages`: the pages of the live topics with new evidence (`docs/DEPLOY.md`, section 6) |
| `systemd/sb-briefs.sh` | `sb-briefs` | every 15 minutes, 07:00 to 21:45, until the pilot's date | `briefs`, the pre-meeting brief pilot; disables its own timer once the pilot has ended (`docs/DEPLOY.md`, section 6) |
| `systemd/sb-mcp.sh` | `sb-mcp` | always on | `python -m src.mcp_server --http` with `BRAIN_ROLE=replica` (`docs/DEPLOY.md`, section 9) |
| `launchd/sb-db-pull.sh` | a LaunchAgent | :45 every hour | the replica pull, and the copy of the encrypted backup archives (`docs/DEPLOY.md`, section 5) |
| `launchd/wait-for-vps.sh` | `sb-db-pull.sh` | before each pull | waits up to 180 s for the producer to answer ssh |
| `launchd/sync-whatsapp-to-vps.sh` | the LaunchAgent template beside it, `com.plessas.whatsapp-sync-vps.plist` | :50 every hour | the WhatsApp snapshot push (`docs/DEPLOY.md`, section 5) |
| `launchd/sync-transcripts-to-vps.sh` | the SessionEnd hook `scripts/hooks/session-end-push.sh` | when a session ends | that session's transcript, to the producer (`docs/DEPLOY.md`, section 5) |

The Mac also runs the weekly restore drill, Sundays at 10:15 in the LaunchAgent
`docs/RESTORE.md` gives; it calls `scripts/backup_db.py --drill` directly, with no
wrapper.

**Environment.** Each systemd unit's `ExecStart` is a host-local shim,
`~/scripts/run-sb-*.sh`, which sets `PATH`, sources the Vertex environment file and
execs the wrapper archived here. The shims hold absolute home paths and recipient
addresses, so they stay host-local, which is the right place for machine-specific
configuration and credentials. `sb-daily-sync.sh`, `sb-health-check.sh` and
`sb-auth-watch.sh` also source `~/.config/healthchecks-ping.env` for `HC_PING_URL`.

**Old headers.** Several systemd wrappers still open with the name of the launchd
job they were before the producer moved to Linux ("launchd surface for ..."), and
several source `~/.zprofile` when it exists. All of `systemd/` runs under systemd on
the producer, where that file may be absent and is then skipped.

The Mac's other `sb-*` wrappers correspond to jobs retired to the VPS and are
deliberately not archived, because committing retired duplicates would only make
it harder to tell which copy matters.

**Do not expect a `*.disabled-migrated-to-vps` plist to mark a retired job.** An
earlier version of this file said the retired plists carry that suffix; zero such
files exist on either Mac today. The renames were lost when the LaunchAgents
directory was re-laid on 2026-08-28, which is exactly why
`scripts/health_check.py::_is_migrated` stopped treating the marker as its only
evidence and added a second, timestamped one (a freshly received replica). Treat
the marker as a convention that may or may not be present, never as the test.

## What the three sync wrappers ask of `src.cli sync`

`sb-outlook-sync.sh` (hourly), `sb-noon-catchup.sh` (noon) and `sb-daily-sync.sh`
(morning) each run the whole sync, Claude Code conversations included, and
`sb-conversation-sync.sh` exports and extracts conversations three times a day
besides. `sync --skip-conversations` (conversations left out) and
`sync --full-dedup` (the people dedup over the whole table, which `sync` already
takes once a day) are for a hand run: no wrapper passes either.

## Two families, not one

`systemd/` and `launchd/` are independent lineages, and no name appears in both.
Until 2026-10-21 `systemd/` also held an old copy of `sb-db-pull.sh` that no unit
ran (the producer runs no pull); it was removed, and with it the one filename the
two families shared. Some differences between the families are legitimate (macOS
and Linux differ on `launchctl`/`systemctl`, `stat`, `date`), so do not expect a
script of one to run on the other's host.

## A skip on a re-auth sentinel is red

A systemd wrapper that leaves work undone because a session is dead
(`~/.second-brain/needs_reauth`, `needs_gcloud_reauth`, `needs_teams_reauth`) exits
69 (EX_UNAVAILABLE), never 0, and still runs whatever does not need that session:
the morning backup, the calendar's facts, the Teams pull, the attachment stages that
read files on disk. Each unit with `Restart=on-failure` lists 69 in
`RestartPreventExitStatus=`, because sb-auth-watch, not a restart, clears the
sentinel. A skip that is not missing work stays 0: another run holding the lock, an
opt-out, a pilot that is off. `docs/DEPLOY.md` section 6 has the drop-ins.

## Verifying a change

CI parses every script with the interpreter named in its shebang, and fails on a
missing or unrecognised one. That catches syntax breakage; it cannot catch
behaviour. After installing a change, exercise the real scheduler path:

```bash
launchctl kickstart -k "gui/$(id -u)/<label>"   # macOS
systemctl --user start <unit>                   # VPS
```

Running the script yourself is the test that lies. A launchd job inherits none of
your terminal's environment or its TCC grants, so a wrapper can pass by hand and
fail on every scheduled run, which is exactly how a document push failed 56
consecutive times over 8 days while every manual check looked fine.

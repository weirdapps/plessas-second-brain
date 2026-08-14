# Scheduler wrappers (archive)

The shell wrappers the schedulers actually invoke, kept here so they are
reviewable, diffable and recoverable. Ingestion is driven by these, not by
`src/cli.py` alone: the wrappers own locking, PATH, timeouts, retry and the
liveness stamps the health check reads.

## This is an archive, not a deploy source

**Editing a file here changes nothing.** The live copies are `~/.local/bin/*.sh`
on each host, and that is what runs. These committed copies exist to answer
"what is production actually running, and what did it look like last month?" —
questions that were previously unanswerable because the scripts lived only on
one laptop's disk.

To change a wrapper: edit the deployed copy, verify it under its real scheduler
(see below), then refresh the archive and commit.

```bash
# refresh from the hosts after a verified change
scp '<vps-host>:~/.local/bin/sb-*.sh' scripts/wrappers/systemd/
cp ~/.local/bin/sb-db-pull.sh ~/.local/bin/sync-documents-to-vps.sh scripts/wrappers/launchd/
```

## Layout

| Directory | Host | Scheduler | Count |
|---|---|---|---|
| `systemd/` | VPS | `systemctl --user` timers | 13 |
| `launchd/` | Mac | LaunchAgents | 2 |

The VPS runs all ingestion. The Mac is a read replica: `sb-db-pull.sh` pulls the
database and embeddings hourly, and `sync-documents-to-vps.sh` pushes the
document roots the other way. The Mac's other `sb-*` wrappers correspond to jobs
retired to the VPS (their plists are renamed `*.disabled-migrated-to-vps`) and
are deliberately not archived — committing retired duplicates would only make it
harder to tell which copy matters.

## What is deliberately absent

**The systemd entry-point shims** (`~/scripts/run-sb-*.sh` on the VPS). Each
service's `ExecStart` points at one of those, which sets PATH, `DISPLAY`, and
sources environment files before exec'ing the wrapper archived here. They hold
absolute home paths and recipient addresses, so they stay host-local — which is
the right place for machine-specific configuration and credentials anyway.

## Two families, not one

`systemd/` and `launchd/` are independent lineages. Where a name appears in both,
the two have drifted — only `sb-reverse-ingest.sh` is currently identical. Some
of that is legitimate (macOS and Linux differ on `launchctl`/`systemctl`, `stat`,
`date`), some is probably rot. They are archived as-is rather than reconciled;
unifying them is a separate exercise with live ingestion at stake.

## Verifying a change

CI parses every script with the interpreter named in its shebang, and fails on a
missing or unrecognised one. That catches syntax breakage; it cannot catch
behaviour. Before refreshing the archive, exercise the real scheduler path:

```bash
launchctl kickstart -k "gui/$(id -u)/<label>"   # macOS
systemctl --user start <unit>                   # VPS
```

Running the script yourself is the test that lies. A launchd job inherits none of
your terminal's environment or its TCC grants, so a wrapper can pass by hand and
fail on every scheduled run — which is exactly how a document push failed 56
consecutive times over 8 days while every manual check looked fine.

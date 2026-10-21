# Backup and restore

`scripts/backup_db.py` writes the backups of `brain.db` (`docs/DEPLOY.md`, section 8,
says where the reference producer runs it). This page is the other half: how to
get a database back out of what it wrote, how to know the copy you got back is
sound, how a file-level backup such as restic should take it, and the weekly drill
that proves a restore still works.

## What the backup produces

Two artefacts, from one run:

| Artefact | Written where | Format |
| --- | --- | --- |
| Local snapshot | `--local-dir`, named `brain-YYYYMMDD.db` | plain SQLite |
| Offsite snapshot | `--offsite-dir`, named `brain-YYYYMMDD.db.zst.enc` | zstd, then AES-256-CBC |

The local snapshot comes from SQLite's online-backup API, not `cp`. Raw-copying
a live WAL-mode database can capture a torn write that will not open, which is
the failure this replaced. Every snapshot is validated with
`PRAGMA integrity_check` before it is kept, and deleted if the check fails, so a
corrupt snapshot never sits on disk pretending to be a good one.

The offsite copy is that snapshot piped through
`zstd -q -c | openssl enc -aes-256-cbc -pbkdf2 -salt -pass file:<key>`. Both
binaries must be on `PATH`: `tools_available()` checks for them and quietly
skips the offsite copy when either is missing, so a missing offsite file is not
by itself something you will be told about.

Retention differs by tier. Local is a rolling `--local-keep` most recent
(default 7). Offsite is grandfather-father-son: `--gfs-daily` dailies (default
14) plus the newest snapshot of each of the last `--gfs-weekly` ISO weeks
(default 8).

## The key

Encryption is passphrase-based, and the passphrase is the contents of the file
named by `--key-file`.

**Keep a copy of that file somewhere that is not an offsite target.** An
encrypted archive whose only key sits on the machine you are restoring because
it burned down is not a backup. The key file is also what scopes the offsite
tier: a host without it, a read replica for instance, skips the encrypted copy
entirely. That is deliberate, so only the authoritative host produces offsite
snapshots.

Note the asymmetry that creates, because it is easy to get wrong: a replica does
not produce offsite snapshots, but it does RECEIVE them, so it can end up holding
a full set of encrypted archives and no key to open them. That was the state on
2026-09-09, when one consumer held 21 snapshots and zero keys while both the
current key and the retired one existed only on the producer. Both are now on the
consumer as well, at mode 600 on a FileVault volume, outside every backup target.
Losing the producer no longer costs the archives.

Keep the RETIRED key. It is the only thing that opens anything written before the
rotation date in its filename.

**Open item: escrow, and an authenticated format.** The keys sit on the producer
and on the consumer, and the consumer keeps them beside the archives it pulls, so
the encryption keeps the archives from neither host. Confirm a copy of each key is
held off both hosts and off every backup target (a password manager or a hardware
key), so neither host holds the only copy. `openssl enc` also carries no MAC:
damage to an archive shows only after it is decrypted, in `quick_check`, which the
drill below now runs weekly. `age`, or restic, would authenticate what they
decrypt. Delete the retired key once the last archive it opens has left the GFS
window. (Audit 2026-10-11, security-privacy-10.)

## Restore

Prerequisites: `zstd`, `openssl` and `sqlite3` on `PATH`, plus the key file.

```bash
# 0. Stop every writer before touching the database, and keep them stopped
#    until the last step. On the producer:
systemctl --user stop 'sb-*.timer' 'sb-*.service'
# On each replica, unload the hourly pull so it cannot copy a half-swapped
# state (launchctl disable alone does not stop a loaded job):
launchctl bootout "gui/$(id -u)/<db-pull label>"
```

A writer still running when the file is swapped keeps the old file open. Its
next commits go into the unlinked old inode and are lost, and deleting a live
`-wal` under it (step 4) throws away what it had not checkpointed yet.

```bash
# 1. Decrypt, then decompress. Note the -d on both.
openssl enc -d -aes-256-cbc -pbkdf2 -pass file:/path/to/backup.key \
  -in brain-20260101.db.zst.enc | zstd -q -d > brain.db
```

A wrong key fails right here, loudly, with `bad decrypt`. It cannot produce a
plausible-looking but wrong database.

```bash
# 2. Verify before trusting it. Both must be clean.
sqlite3 brain.db 'PRAGMA quick_check;'         # expect exactly: ok
sqlite3 brain.db 'PRAGMA foreign_key_check;'   # expect no output at all
```

```bash
# 3. Assert it is the corpus you think it is, not an empty shell.
sqlite3 brain.db "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table';"
sqlite3 brain.db "SELECT COUNT(*) FROM emails;"
sqlite3 brain.db "SELECT key, value FROM sync_metadata WHERE key IN ('last_sync_date', 'mail_export_ok_at');"
```

`last_sync_date` is when a sync last ran, and `mail_export_ok_at` is when the
Inbox export last succeeded; the MCP `stats` tool reports the older of the two as
`data_as_of`. Check both before serving from a restored copy: everything after
the older one is missing, and nothing else in the file will tell you.

```bash
# 4. Put it in place with the WAL sidecars removed. They belong to the old
#    file, and a stale -wal replayed over a fresh database is a corruption,
#    not a recovery. With BRAIN_DATA_DIR unset the data home is the repo's
#    data/ directory, which may be a symlink: resolve it, and check the path
#    before deleting anything under it.
DATA="${BRAIN_DATA_DIR:-$(cd /path/to/plessas-second-brain/data && pwd -P)}"
ls -la "$DATA/brain.db"
rm -f "$DATA/brain.db-wal" "$DATA/brain.db-shm"
mv brain.db "$DATA/brain.db"
```

The embedding index is not in the database. `embeddings.npz` is a separate file
in the same data home, is not covered by these snapshots, and semantic search
degrades to keyword-only without it.

**Discard an `embeddings.npz` newer than the restored snapshot; never keep it.**
Row ids are `INTEGER PRIMARY KEY` without `AUTOINCREMENT`, so after the restore
new rows reuse the ids of the rows the snapshot lost. The newer index already
maps those ids to the lost rows' vectors, an incremental `embed` skips any id it
already holds, and semantic search then answers with the wrong rows for good.
Keep an index only if it was taken at the same moment as the snapshot.
Otherwise rebuild it, from the repo directory:

```bash
# 5. Rebuild the embedding index against the restored rows.
mv "$DATA/embeddings.npz" "$DATA/embeddings.npz.pre-restore"
python -m src.cli maint vectors-rebuild --abandon      # a side build from before the loss, if any
python -m src.cli maint vectors-rebuild --estimate     # texts, tokens and dollars; sends nothing
python -m src.cli maint vectors-rebuild                # every kind, then the swap; resumes
```

The rebuild embeds every summary (emails, attachments, conversations, Teams threads,
WhatsApp sessions) and every chunk of the long PDF, Word and PowerPoint attachments,
cutting the chunks first (`attachment_chunks`, schema v39), in this code's space
(gemini-embedding-2 at 768 dimensions; README, "Database schema", Vector index). It
builds into `$DATA/embeddings.rebuild/`, a part or a batch job at a time, and only
the finished index is put in place, so a run stopped by a signal, an error or
`--token-budget N` carries on where it was when run again. It sends Gemini batch jobs
(half the online price), rolling: the Gemini API caps the tokens a key may have
queued in embedding batch jobs (500,000 at tier 1, 5 million at tier 2), and a job
that would pass the cap waits until earlier ones finish. `--online` sends 100 texts a
call instead, as it does on Vertex AI, which has no embedding batch jobs.

Measured on the reference replica, 2026-10-16: 143,801 summary texts (58 M characters,
about 16 M tokens at 3.56 characters a token) and 86,133 chunks from 5,733 documents
(391 M characters, about 147 M tokens at 2.66), so about $16 as batch jobs and $33
online. A tier-2 key cleared about 5 million tokens every 2 to 3 minutes in batch
mode, so the whole rebuild takes about an hour and a half; the estimate prints the
counts for the store at hand.

Run it on the producer with the environment its scheduled jobs use. The service is
chosen as it is everywhere else (`src.config.embed_backend`): `BRAIN_EMBED_BACKEND`
when set, else the Gemini API when `GEMINI_API_KEY` is set, else Vertex AI. Where the
Vertex project refuses the embedding model, export `BRAIN_EMBED_BACKEND=gemini` and
`GEMINI_API_KEY` before step 5. The writers are stopped (step 0), so nothing waits on
the rebuild; had they been running, they would add no vector while it holds its lock.

The export cursors are not in the snapshot either, and they are now ahead of
it. Each `outlook_sync*.json` under `$DATA/state` still holds the newest mail
the lost database had seen, so the next run lists only mail after that and the
gap between `last_sync_date` and the cursor is never fetched again. Before
restarting, rewind `last_seen_received_at` in each of them; the loader skips
mail it already holds by message id.

The two timestamps are in different zones, so do not copy one into the other.
`last_sync_date` is the producer's local wall-clock time with no zone
(`src/pipeline/sync.py` writes `datetime.now()`), while `last_seen_received_at` is UTC
with a trailing `Z`. Appending a `Z` to the local time moves the cursor hours
forward, and the mail in those hours is skipped for good. Subtract at least one
export interval as well, because mail received after the last export before
that load is missing from the snapshot too. Convert on the producer, whose
local zone is the one `last_sync_date` was written in, and raise the hour if
the export runs less often:

```bash
python3 -c 'import sys, datetime as d; t = d.datetime.fromisoformat(sys.argv[1]).astimezone(d.timezone.utc) - d.timedelta(hours=1); print(t.strftime("%Y-%m-%dT%H:%M:%SZ"))' "<last_sync_date>"
```

Write the result into each file. Check any other source you stage from for a
cursor of its own.

```bash
# 6. Start the writers again. On the producer:
systemctl --user start 'sb-*.timer'
# Step 0 stopped the long-running services too: start the HTTP MCP server
# (DEPLOY section 9) and anything else that reads the store, if you run them.
systemctl --user start sb-mcp.service
# On each replica, load the pull again:
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/<db-pull plist>
```

`scripts/backup_db.py` also exposes `decrypt_decompress()` if you would rather
do step 1 in Python than in a shell pipeline. It is the exact inverse of the
`compress_encrypt()` that wrote the file.

## restic: the copy off both hosts

The local snapshots and the encrypted archives both sit on the producer's data
disk, and the archives' only other copy is on the consumer that pulls them. A
file-level backup of the producer to a third place (the reference producer runs
restic nightly) is what survives losing both hosts, so it has to hold a database
that opens.

**Back up the snapshot, never the live file.** `brain.db` runs in WAL mode and is
written while a backup reads it, so a copy of the live file can be torn, and a
`-wal` read a moment later belongs to another instant. The daily
`backups/brain-YYYYMMDD.db` that `backup_db.py` writes is consistent and passed
`PRAGMA integrity_check`, so that is the copy restic should hold. Keep the data
home among the backup's paths and exclude the live file, its sidecars and the
encrypted archives (incompressible, so restic would store each one whole, and the
consumer already holds them). In restic's excludes file, with your data home's
absolute path:

```text
/path/to/data-home/brain.db
/path/to/data-home/brain.db-wal
/path/to/data-home/brain.db-shm
/path/to/data-home/backups/offsite
```

If the excludes file leaves out `backups/` wholesale, delete that line: the
snapshots under it are the only consistent copy. Consecutive snapshots share most
of their pages, so deduplication stores what changed between them rather than
each one whole; the first run after the change reads every snapshot once. restic
holds whichever snapshot is newest when it runs, so it trails the live database by
up to a day.

**The attachment tree.** An excludes file that also leaves out `attachments/` and
`sharepoint/` (files are inputs, [`DEPLOY.md`](DEPLOY.md) section 10) drops every
file left on disk, and the database holds only what was read out of them. Most have
another master, the mailbox or SharePoint, for as long as it keeps them; some have
none. On the reference producer, 2026-10-21: 12,629 files, 26.9 GiB. Of the files
attached to stored mail, and so in the mailbox while it keeps the message, 14.0 GiB are
encrypted originals the store could not read, 9.9 GiB were kept by review for text too
thin for their size, 1.1 GiB were skipped and 0.3 GiB await the sweep; `sharepoint/`
was empty between fetches. The files with no master, documents ingested from disk and
mail whose message row is gone, were hardlinked once into a `kept-originals/`
directory outside `attachments/`, which the backup keeps at no extra space on disk:
770 files, 1.3 GiB. Another 81 files of that kind (154 MiB, registered April to
September) are not linked, nor are 111 files (52 MiB) that no row names: check that
each has a copy elsewhere and link those that do not. Nothing links new ones, so
re-run the linking after each review, or keep `attachments/` in the backup until
something does it on its own.

To restore from it, on the producer, as the user the restic job runs as and with
its repository and password in the environment. `--host` matters on a repository
other machines back up to as well: `latest` is otherwise the newest snapshot of
any of them.

```bash
umask 077
restic snapshots --host <producer>
restic ls --host <producer> latest /path/to/data-home/backups | grep 'brain-[0-9]*\.db$'
restic dump --host <producer> latest /path/to/data-home/backups/brain-YYYYMMDD.db > brain.db
```

Then continue at step 2: the snapshot is plain SQLite, so there is nothing to
decrypt or decompress, and steps 2 to 6 apply as written. restic's own
`check --read-data-subset` proves the repository reads back, not that the database
in it opens, and the drill below covers the encrypted archives only, so run this
path by hand now and then.

## Exercise it

An untested restore is a hypothesis, so the drill is a scheduled job.
`scripts/backup_db.py --drill` decrypts the newest archive in `--offsite-dir` into
a private temporary directory, runs `PRAGMA quick_check` on it, and compares the
row counts of `emails`, `attachments`, `teams_messages` and `decisions` with the
database named by `--db`, each within 10% of the larger of the two
(`--drill-tolerance`, a share of at least 0 and below 1; the archive is up to a
day older, so the counts are close, not equal). It only reads that database
(`mode=ro`, and `immutable=1` on a replica), and removes the decrypted copy
however it ends.

A pass writes `~/.second-brain/restore-drill.stamp` (or `--drill-stamp`, or
`$BRAIN_RESTORE_DRILL_STAMP`): the time on line 1, which the health check's
Backups row ages and warns on after 35 days, then the archive's name and the
counts. A failure exits 1 and leaves the stamp alone. With `--hc-slug` and
`HC_PING_URL` set, a pass pings the slug and a failure pings its `/fail`.

Run it weekly on the consumer that pulls the archives, which holds the keys, the
archives and a replica to compare with:

```bash
cd /path/to/plessas-second-brain
.venv/bin/python scripts/backup_db.py --drill \
  --offsite-dir ~/second-brain-backups/offsite --key-file ~/.second-brain/backup.key \
  --db data/brain.db --hc-slug brain-restore-drill
```

It needs `zstd` and `openssl` on `PATH`, as the manual steps do, and room for the
whole database where it decrypts (`--work-dir`, else the system temporary
directory). Below, a LaunchAgent for it, Sundays at 10:15, clear of the pull at
:45. launchd's `PATH` has no Homebrew, hence the `export`. It expands neither `~`
nor `$HOME` in its keys, hence `__HOME__`. And `HC_PING_URL` is a secret, so it
comes from a file only the owner can read, never from the plist; without that
file the drill still runs and records, and only the ping is skipped.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.second-brain.restore-drill</string>
    <key>WorkingDirectory</key>
    <string>__HOME__/SourceCode/plessas-second-brain</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; if [ -r "$HOME/.config/healthchecks-ping.env" ]; then set -a; . "$HOME/.config/healthchecks-ping.env"; set +a; fi; exec .venv/bin/python scripts/backup_db.py --drill --offsite-dir "$HOME/second-brain-backups/offsite" --key-file "$HOME/.second-brain/backup.key" --db data/brain.db --hc-slug brain-restore-drill</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>10</integer>
        <key>Minute</key>
        <integer>15</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>Umask</key>
    <integer>63</integer>
    <key>StandardOutPath</key>
    <string>__HOME__/.second-brain/logs/restore-drill.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/.second-brain/logs/restore-drill.log</string>
</dict>
</plist>
```

Saved as `restore-drill.plist`, install it and run the first drill at once:

```bash
AGENT=~/Library/LaunchAgents/com.example.second-brain.restore-drill.plist
sed "s|__HOME__|$HOME|g" restore-drill.plist > "$AGENT"
plutil -lint "$AGENT"
launchctl bootstrap "gui/$(id -u)" "$AGENT"
launchctl kickstart "gui/$(id -u)/com.example.second-brain.restore-drill"
tail -3 ~/.second-brain/logs/restore-drill.log   # "restore drill OK on brain-..."
```

A restore you exercise by hand, by the steps above, is recorded the same way:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ > ~/.second-brain/restore-drill.stamp   # or $BRAIN_RESTORE_DRILL_STAMP
```

Last exercised by hand: 2026-09-09, twice. From 2026-10 the drill above exercises
it every week once its agent is installed, and the stamp says when it last passed.

First on a synthetic database, which validates the command sequence but not any
archive. Then, once a consumer had its own key, against the newest REAL offsite
snapshot on a consumer host with no access to the producer's copy of anything:

```text
brain-20260909.db.zst.enc, 858,796,160 bytes
decrypt + decompress          ~2 s      -> 3,333,738,496 bytes
PRAGMA quick_check            ok
PRAGMA foreign_key_check      clean
tables 67, emails 79,264, last_sync_date 2026-09-09T01:42:10
```

That second run is the one that answers the question this page exists for: can
this machine, alone, turn an archive back into a working brain. Until the keys
were copied it could not, and nothing said so.

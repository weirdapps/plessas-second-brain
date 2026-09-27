# Backup and restore

`scripts/backup_db.py` is the only thing that copies `brain.db` safely. This
page is the other half: how to get a database back out of what it wrote, and how
to know the copy you got back is sound.

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
sqlite3 brain.db "SELECT value FROM sync_metadata WHERE key = 'last_sync_date';"
```

`last_sync_date` is the honest age of the archive, and it is what the MCP
`stats` tool reports as `data_as_of`. Check it before serving from a restored
copy: everything after that timestamp is missing, and nothing else in the file
will tell you.

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
python -m src.cli embed --force   # emails, attachments, conversations
python -c 'from src.config import DEFAULT_DB; from src.store.schema import get_connection; from src.store.embeddings import build_teams_index; print(build_teams_index(get_connection(str(DEFAULT_DB))))'   # Teams threads
```

The export cursors are not in the snapshot either, and they are now ahead of
it. Each `outlook_sync*.json` under `$DATA/state` still holds the newest mail
the lost database had seen, so the next run lists only mail after that and the
gap between `last_sync_date` and the cursor is never fetched again. Before
restarting, set `last_seen_received_at` in each of them to a time at or before
`last_sync_date`; the loader skips mail it already holds by message id. Check
any other source you stage from for a cursor of its own.

```bash
# 6. Start the writers again. On the producer:
systemctl --user start 'sb-*.timer'
# On each replica, load the pull again:
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/<db-pull plist>
```

`scripts/backup_db.py` also exposes `decrypt_decompress()` if you would rather
do step 1 in Python than in a shell pipeline. It is the exact inverse of the
`compress_encrypt()` that wrote the file.

## Exercise it

An untested restore is a hypothesis. Run the sequence above against the newest
offsite snapshot on whatever schedule you can live with, and check that step 2
comes back clean and step 3's counts land where you expect.

Last exercised: 2026-09-09, twice.

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

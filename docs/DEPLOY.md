# Deployment

The engine runs from this repo plus your own environment, no external private
repo is required. It works on macOS (launchd) and Linux (systemd/cron), either
on a single host or split across two (see "Topology" below).

## 1. Prerequisites

- Python 3.12+.
- `sqlite3` on `PATH`. `scripts/health_check.py` and the replica pull both shell
  out to it.
- `tesseract`, plus the language data for the languages your attachments are
  written in.
- `zstd` and `openssl`, if you want encrypted offsite backups.

**Tesseract is not optional if you ingest attachments.** Attachment OCR calls
`pytesseract.image_to_string(img, lang="eng+ell")`
(`src/extract/attachment_extractors.py`), so both the English and the Greek
traineddata must be installed or every image and every scanned PDF fails. `pip`
installs the `pytesseract` wrapper, never the binary and never a language pack.
In a corpus with scanned documents in it, OCR is the most common extraction
method of all.

```bash
brew install tesseract tesseract-lang                  # macOS
sudo apt-get install tesseract-ocr tesseract-ocr-ell   # Debian/Ubuntu
tesseract --list-langs | grep -x ell                   # verify the Greek pack
```

`scripts/backup_db.py` checks for `zstd` and `openssl` together
(`tools_available()`) and skips the encrypted offsite copy when either is
missing. The local snapshot still succeeds, so a host without them reports a
good backup and produces no offsite artefact. See [`RESTORE.md`](RESTORE.md).

Optional, for Microsoft 365 ingestion. Each is a separate open-source CLI with
its own login, and they share no session:

| Surface | CLI | Path override |
| --- | --- | --- |
| Mail, attachments, calendar | [`outlook-cli`](https://github.com/weirdapps/outlook-access) | `OUTLOOK_CLI_PATH` |
| Teams chats and channels | [`teams-cli`](https://github.com/weirdapps/teams-access) | none, must be on `PATH` |
| SharePoint reference links | [`sharepoint-cli`](https://github.com/weirdapps/sharepoint-access) | `SHAREPOINT_CLI_PATH` |

Set the path overrides for any process that does not inherit an interactive
shell's `PATH`: MCP servers, launchd agents and systemd units all qualify.
Everything else (local docs, URLs, YouTube, Claude Code conversations, your own
exporter) works without any of the three.

## 2. Install

```bash
git clone https://github.com/weirdapps/plessas-second-brain.git
cd plessas-second-brain
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The `dev` extra installs `pytest` and `pytest-cov` only. `ruff` is deliberately
not in it because CI pins an exact version; run it with
`uvx ruff@0.15.13 check .` (see `README.md`, "Lint and format").

## 3. Configure

```bash
cp .env.example .env      # then edit .env
```

Required: `BRAIN_USER_NAME`, `BRAIN_USER_ROLE`, `BRAIN_USER_EMAIL_PATTERN`, and one
extraction path (Vertex ADC, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`). See
`.env.example` for the full list.

**Data home.** By default the DB and all ingested data live in `<repo>/data`. For
a scheduled deployment, point them at a stable, checkout-independent location so
they survive repo moves and upgrades:

```bash
export BRAIN_DATA_DIR="$HOME/.second-brain/data"
```

Everything (DB, attachments, staging, embeddings, sharepoint) derives from
`BRAIN_DATA_DIR`.

**Write it as an absolute path in `.env`.** A shell expands `~` and `$HOME` in
the cron recipe below; systemd's `EnvironmentFile=` and launchd's
`EnvironmentVariables` do neither, and `src/config.py` does not call
`expanduser()`. The two scheduling paths would otherwise disagree about where
the database lives, and the systemd one would create a directory literally
named `~` in its working directory.

## 4. Bootstrap and verify

```bash
mkdir -p "$BRAIN_DATA_DIR/staging" "$BRAIN_DATA_DIR/extracted"
python -m src.cli load    # creates brain.db, then runs every migration
python -m src.cli stats   # opens it; every count is 0 on a fresh host
python -m src.cli sync    # one ingest, extract, load cycle
```

`load` is the only command that creates the database. It stamps schema version 0
and calls `run_migrations`, so a store built this way ends up with exactly the
tables a store that grew through the migrations has. Everything else assumes the
file already exists: `stats`, `sync` and `migrate` all print `Database not
found` and exit 1 against a data home that has never been loaded.

Create `staging/` and `extracted/` first. Without them `load` still creates the
database, but then exits 1 because it has nothing to read, which is a confusing
signal to leave in a bootstrap script.

## 5. Topology: one host or two

Everything above describes one host that ingests and serves from the same
`BRAIN_DATA_DIR`. The other shape splits the two roles:

- **Producer.** One always-on Linux host runs every ingest job on
  `systemd --user` timers and owns the only writable `brain.db`.
- **Consumer.** Each workstation is a read replica. An hourly job rsyncs
  `brain.db` and `embeddings.npz` down from the producer, and the local MCP
  server reads that copy. `scripts/wrappers/launchd/sb-db-pull.sh` is the
  reference implementation: it checkpoints the producer's WAL, takes a
  `.backup` snapshot there, rsyncs the snapshot rather than the live file
  (copying a WAL-mode database in place can capture a torn read), runs
  `PRAGMA quick_check` on the result, and writes a UTC timestamp to
  `~/.second-brain/db-pull.stamp`.

**On the producer, enable linger.** `systemd --user` timers are killed when the
user's last session ends, so without this every ingest job stops silently as
soon as you close the SSH connection you set it up from:

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger   # must print Linger=yes
```

A replica is only as current as its last successful pull, and a stale replica
answers exactly like a live one unless something says otherwise. Two things do:
the `stats` MCP tool returns `data_as_of`, `age_hours` and `stale`, and `recall`
attaches a `_stale_warning` when the copy is behind.

`scripts/wrappers/` archives the wrapper scripts both sides actually run, with
its own README.

## 6. Schedule

The pipeline is just CLI commands, so schedule them however you like. Source your
`.env` first so the job inherits `BRAIN_DATA_DIR` and credentials.

**cron** (hourly incremental sync, daily embed):

```cron
7 * * * *  cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python -m src.cli sync  >> ~/.second-brain/logs/sync.log 2>&1
23 3 * * * cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python -m src.cli embed >> ~/.second-brain/logs/embed.log 2>&1
```

**Linux, systemd (user) timer.** A wrapper that sources `.env` and runs the CLI:

```ini
# ~/.config/systemd/user/sb-sync.service
[Service]
Type=oneshot
WorkingDirectory=%h/plessas-second-brain
EnvironmentFile=%h/plessas-second-brain/.env
ExecStart=%h/plessas-second-brain/.venv/bin/python -m src.cli sync
```

```ini
# ~/.config/systemd/user/sb-sync.timer
[Timer]
OnCalendar=hourly
[Install]
WantedBy=timers.target
```

Enable: `systemctl --user enable --now sb-sync.timer`, then confirm linger is on
(section 5).

**macOS, launchd.** A `LaunchAgent` in `~/Library/LaunchAgents/` running a
wrapper that `cd`s into `$BRAIN_REPO`, sources `.env`, and runs
`python -m src.cli sync` on `StartInterval` (or `StartCalendarInterval`). Set
`BRAIN_LABEL_PREFIX` to namespace the labels.

`sync` covers mail, extraction, load, attachments, people dedup, embeddings,
Claude Code conversations and inline images. It does not cover Teams, calendar,
news, SharePoint or the filesystem scan, so `calendar-sync`, `teams-sync`,
`news-sync`, `process-sharepoint` and `reverse-ingest` each want their own
schedule. `python -m src.cli --help` lists every subcommand.

## 7. Health check (optional)

`scripts/health_check.py` reports per-source freshness and job status, and can
email a summary (needs `outlook-cli` and `HEALTH_EMAIL_TO`):

```bash
HEALTH_EMAIL_TO=you@example.com python scripts/health_check.py --email
```

Other flags: `--email-if-issues` (silent on a healthy run), `--fix` (attempt the
auto-fixes), and `--hc-ping`.

**Wire up the dead man's switch.** A health check that emails you is worth
exactly as much as the scheduler that runs it. If the host is off, or the timer
never fires, silence is indistinguishable from health. `--hc-ping` reports data
freshness to a [Healthchecks](https://healthchecks.io) check, which alarms on
the *absence* of a ping, so a dead scheduler becomes loud instead of invisible.
It needs `HC_PING_URL` in the environment: the ping base URL for your project.

Two rules for that variable. Treat it as a secret, because anyone holding it can
mark your checks green; this is why `health_check.py` logs curl's exit code and
never its stderr, which contains the URL. And read it from the environment only,
never as a CLI argument, so it cannot leak into a process listing or a committed
wrapper.

Pass `--hc-ping` from the scheduled run and never from an ad hoc one: a manual
invocation that pings resets the switch and hides a scheduler that has already
stopped. That is why the flag is off by default. `scripts/backup_db.py --hc-slug
<slug>` reports the nightly backup on its own separate check, for the same
reason: "the sync ran" and "a backup was written" are different facts, and only
the second one is worth a green check on the backup.

## 8. Backup and restore

`scripts/backup_db.py` takes an MVCC-consistent snapshot of a live `brain.db`,
verifies it, and optionally writes a zstd-compressed, AES-256-encrypted offsite
copy. The restore path, its prerequisites, and the checks that tell a good
archive from a corrupt one are in [`RESTORE.md`](RESTORE.md).

## 9. Serve to Claude Code

Register the MCP server once (see `README.md`, "Register with Claude Code"). It
reads whichever `brain.db` sits under the local `BRAIN_DATA_DIR`.

On a single host that is the database your scheduled jobs write, so queries see
the latest ingest. On a replica it is whatever the last pull left behind: check
`~/.second-brain/db-pull.stamp`, or the `data_as_of` and `age_hours` fields the
`stats` tool returns, before trusting an answer about anything recent.

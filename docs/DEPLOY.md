# Deployment

The engine runs from this repo plus your own environment, no external private
repo is required. It works on macOS (launchd) and Linux (systemd/cron), either
on a single host or split across two (see "Topology" below).

## 1. Prerequisites

- Python 3.12+, and [`uv`](https://docs.astral.sh/uv/), which installs from the
  committed `uv.lock`.
- `sqlite3` on `PATH`, on both hosts of a two-host setup: the replica pull runs
  `.backup` on the producer and `PRAGMA quick_check` on the replica, and the
  manual restore in [`RESTORE.md`](RESTORE.md) uses it. Everything else opens the
  database through Python's own `sqlite3` module.
- `curl`, for the health check's Healthchecks pings (section 7).
- `unzip` on a replica. The pull checks a new `embeddings.npz` with `unzip -t`
  before it replaces the old one. macOS ships it.
- `tesseract`, plus the language data for the languages your attachments are
  written in.
- `antiword` (or `catdoc`) on Linux, for legacy Word `.doc` attachments. macOS
  uses its built-in `textutil`. Without any of the three every `.doc` is
  recorded as skipped ("No legacy .doc converter available"), which no failure
  count reports.
- `zstd` and `openssl`, if you want encrypted offsite backups.

**Tesseract is not optional if you ingest attachments.** Attachment OCR reads
Greek first, `pytesseract.image_to_string(img, lang="ell+eng")`, and reads a page
that comes out as Latin script again with `lang="eng+ell"`
(`src/extract/attachment_extractors.py`), so both the English and the Greek
traineddata must be installed or every image and every scanned PDF fails. `pip`
installs the `pytesseract` wrapper, never the binary and never a language pack.
In a corpus with scanned documents in it, OCR is the most common extraction
method of all.

```bash
brew install tesseract tesseract-lang                  # macOS
sudo apt-get install tesseract-ocr tesseract-ocr-ell   # Debian/Ubuntu
sudo apt-get install antiword                          # Debian/Ubuntu, legacy .doc
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
uv sync --frozen --no-build   # .venv from uv.lock; add --extra dev for the tests
source .venv/bin/activate     # the commands below run its python
```

For a venv outside the checkout (the reference producer keeps it in
`~/.venvs/second-brain`), set `UV_PROJECT_ENVIRONMENT` to its path first. A venv
uv builds has no `pip`, so a deploy script should run `uv sync`, not
`pip install`. `--no-build` refuses to run a package's `setup.py`, as CI does.
`pyproject.toml` declares no build system, so the project itself is not
installed: everything runs from the checkout (`./brain`, `./run_mcp.sh`, or the
venv's `python -m src.cli` from the checkout's root). `pip install -e .` would
ignore `uv.lock` and put `src/` itself, not the checkout, on the module path.

The `dev` extra holds the test tools. `ruff` is deliberately not in it because
CI pins an exact version; run it with `uvx ruff@0.16.8 check .` (see
`README.md`, "Lint and format").

## 3. Configure

```bash
cp .env.example .env      # then edit .env
```

Required: `BRAIN_USER_NAME`, `BRAIN_USER_ROLE`, `BRAIN_USER_EMAIL_PATTERN`, and one
extraction path: Vertex ADC with `VERTEX_SDK_PROJECT`, `CLAUDE_EXTRACT_MODEL` and
`VERTEX_SDK_REGION` (neither the model nor the region has a default, and the first
Claude call stops with a message naming them until both are set); `GEMINI_API_KEY`
with `BRAIN_EXTRACT_ENGINE=gemini`; or the direct Anthropic API, which takes
`ANTHROPIC_API_KEY` together with `BRAIN_ALLOW_DIRECT_API=1` and is used only where
no Vertex project is set. The README's "Configuration" lists every setting the
pipeline and the server read; `.env.example` is a starting point with the common
ones, and sections 7 and 9 below list the health check's.

`BRAIN_USER_EMAIL_PATTERN` lists every address the owner sends from, primary and
aliases, separated by commas: `BRAIN_USER_EMAIL_PATTERN=first.last@example.com,alias@example.com`.
Each is matched whole and case-blind, never as a substring, so an alias that is
not listed is not the owner's (README, "Identity"). After changing it, run
`python -m src.cli maint classify-sources` on the producer; the next calendar sync
refreshes the self flags of the meetings it lists (the week behind and the 30 days
ahead); `python -m src.cli maint calendar-backfill` re-lists older months.

Nothing in the code reads `.env`: the scheduling recipes in section 6 source it
(`set -a; . ./.env`, `EnvironmentFile=`). The MCP server inherits the
environment `claude` was launched with, which sources no `.env` and, from a GUI
or an IDE, no shell profile, so put the identity settings
(`BRAIN_USER_NAME`, `BRAIN_USER_ROLE`, `BRAIN_USER_EMAIL_PATTERN`,
`SHAREPOINT_HOST`) in `~/.config/second-brain/env` too; `src/config.py` reads
that file at import, and ignores any other key in it. The server's own settings,
such as `GEMINI_API_KEY` and `BRAIN_EMBED_BACKEND`, go in the owner-only file
`run_mcp.sh` sources, `$BRAIN_DATA_DIR/env` or else `~/.second-brain/env`
(README, "Run the MCP server").

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

**Upgrading a host that already set `BRAIN_DATA_DIR`.** The Outlook exporter
used to keep its cursors, staged batches and attachments in `<repo>/data`
whatever the variable said, and extraction never read those batches. The
cursors are carried over on the next run, one per folder, when the data home
holds none; with the timers stopped, move the rest yourself:
`<repo>/data/staging/batch-*.json` and `<repo>/data/attachments/*` into the same
places under `$BRAIN_DATA_DIR`. A run that exits 7 has no cursor: move it
across, and do not answer with `--bootstrap`, which fetches only the newest 100
messages. A replica's pull (`sb-db-pull.sh`) reads the producer's
`<repo>/data`: edit its `REMOTE_DATA` to the new data home too. The wrappers in
`scripts/wrappers/` are an archive, so copy the updated `sb-outlook-sync.sh` and
`sb-daily-sync.sh` into `~/.local/bin` as well. To
roll back, rename `<repo>/data/state/*.json.carried` back to `*.json`, after
copying across any newer cursor from `$BRAIN_DATA_DIR/state`.

## 4. Bootstrap and verify

```bash
mkdir -p "$BRAIN_DATA_DIR/staging" "$BRAIN_DATA_DIR/extracted"
python -m src.cli load         # creates brain.db, then runs every migration
python -m src.cli tool stats   # opens it; every count is 0 on a fresh host
```

Mail reaches the store in two steps, and `sync` is only the second:

1. **Stage.** With Microsoft 365, `python -m src.export.outlook_export --folder Inbox --bootstrap`
   stages mail through `outlook-cli`: the first run takes the 100 most recent
   messages, later runs continue from the cursor it saves. Each folder gets its
   own cursor file under `$BRAIN_DATA_DIR/state` without `--state-path`
   (`outlook_sync.json` for Inbox, `outlook_sync_archive.json` for Archive,
   `outlook_sync_sent.json` for Sent Items), and a run given a cursor saved for
   another folder exits 9 without touching it. Any other source writes staging
   batches itself; see "Bring your own source" in the README.
2. **Extract and load.** On a fresh store, `python -m src.extract.local && python -m src.cli load`,
   because `sync` skips an empty store. From then on `python -m src.cli sync`
   does both.

`load` is the only command that creates the database. It stamps schema version 0
and calls `run_migrations`, so a store built this way ends up with exactly the
tables a store that grew through the migrations has. Everything else assumes the
file already exists: `tool`, `sync` and `maint migrate` all report the database
not found and exit 1 against a data home that has never been loaded.

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
  (copying a WAL-mode database in place can capture a torn read) into
  `brain.db.new`, runs `PRAGMA quick_check` on that, and only then renames it
  over `brain.db`. `embeddings.npz` follows the same way, checked with
  `unzip -t`, and only beside a database that passed. A copy that fails its
  check is discarded and the replica stays as it was; the run logs the
  `ERROR`, exits 1 and leaves the stamp alone. On success it writes a UTC
  timestamp to `~/.second-brain/db-pull.stamp`. The same run copies the
  producer's encrypted backup archives (`backups/offsite/brain-*.db.zst.enc`
  under its data home) to `~/second-brain-backups/offsite` and prunes them to
  the same retention, which is what the weekly restore drill decrypts
  ([`RESTORE.md`](RESTORE.md), "Exercise it").

**An unchanged producer costs no snapshot.** The `.backup` rewrites the whole
database on the producer. When the producer's `brain.db` has the mtime and size
the last good pull recorded (`~/.second-brain/db-pull.source`, written beside
the stamp), an empty `-wal`, and the consumer's snapshot is still there, the run
reuses that snapshot instead. Any failed run deletes the record, so the next one
snapshots afresh.

**Run the pull around the clock.** The `stats` tool calls a copy older than three
hours stale, so a pull that stops overnight leaves every early-morning answer
flagged stale. Schedule it hourly all day: in the agent's plist, replace the
`StartCalendarInterval` array of per-hour entries with this one dictionary, which
fires at minute 45 of every hour:

```xml
<key>StartCalendarInterval</key>
<dict>
    <key>Minute</key>
    <integer>45</integer>
</dict>
```

Then reload it (`launchctl disable` alone does not stop a loaded job):

```bash
PLIST=~/Library/LaunchAgents/<db-pull label>.plist
launchctl bootout "gui/$(id -u)/<db-pull label>"
# edit "$PLIST" as above
plutil -lint "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# one calendar trigger left, whose descriptor names only "Minute" => 45
launchctl print "gui/$(id -u)/<db-pull label>" | grep -A3 descriptor
```

Install the current wrapper before changing the schedule
(`cp scripts/wrappers/launchd/sb-db-pull.sh ~/.local/bin/`): it is what makes a
night hour in which nothing wrote cost the producer no snapshot.

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

### Claude Code conversations from a workstation

The producer reads Claude Code transcripts from its own `~/.claude/projects`, so
each workstation sends a session's transcript there when the session ends. A
Claude Code `SessionEnd` hook, `scripts/hooks/session-end-push.sh`, reads the
hook's input on stdin, leaves agent sessions out (a subagent's, an in-process or
split-pane teammate's, a scripted `claude -p` run's) and starts
`scripts/wrappers/launchd/sync-transcripts-to-vps.sh` in the background, detached,
so the session's 1.5 s hook budget is never spent on the network. The push sends
that one transcript to the same path on the producer, owner-only, and never the
session's `subagents/` directory. It keeps the WhatsApp push's contract:
`~/.second-brain/transcripts-sync.stamp` on both hosts after a push,
`transcripts-sync.fail` with the reason when one fails, and a producer it cannot
reach skipped quietly until the gap outlives 24 hours or 12 runs. A transcript it
could not send waits in `~/.second-brain/transcripts-sync.pending` and goes with
the next push. Its log is `~/Library/Logs/transcripts-sync-vps.log`.

```bash
cp scripts/hooks/session-end-push.sh scripts/wrappers/launchd/sync-transcripts-to-vps.sh ~/.local/bin/
```

Then add the hook to the `hooks` block of `~/.claude/settings.json`. If a
`SessionEnd` entry is there already, add the inner hook to its `hooks` list. The
test keeps a host where the script is not installed quiet:

```json
"SessionEnd": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "[ -f \"$HOME/.local/bin/session-end-push.sh\" ] && bash \"$HOME/.local/bin/session-end-push.sh\" || true",
        "timeout": 5
      }
    ]
  }
]
```

The push names the producer `vps` (an `~/.ssh/config` host); set
`SB_TRANSCRIPTS_VPS` to change it. Check it after the next session ends: the
stamp updates, and the log says `pushed 1 transcript(s)`.

Remove any older capture path. The `ingest-conversation-incremental` subcommand
and `scripts/conversation-capture.sh` are gone, and a hook that ingested
transcripts on the workstation is refused there anyway, since a replica writes
nothing: drop any `SessionStart` or `Stop` hook that called them. A periodic copy
of `~/.claude/projects` to the producer can go too once the stamp shows the hook
working; note that `ingest-session-notes` then no longer sees subagent
transcripts, which only such a copy carried.

Conversations stored before 2026-10-11 hold every user-role turn as the owner's:
`relabel-conversations` sorts them once (section 12).

### WhatsApp from the Mac that runs the bridge

The README's "WhatsApp" section says what the snapshot holds and where it goes.
Two pieces run it: a LaunchAgent on the Mac that runs the bridge, at :50, and a
timer on the producer, at :55. The WhatsApp tables arrive with schema v26, which
any command that writes applies.

On the **producer**, after pulling this repository:

```bash
install -m 755 scripts/wrappers/systemd/sb-whatsapp-sync.sh ~/.local/bin/
# ~/scripts/run-sb-whatsapp-sync.sh: a copy of run-sb-teams-sync.sh that execs sb-whatsapp-sync.sh
```

```ini
# ~/.config/systemd/user/sb-whatsapp-sync.service
[Unit]
Description=second-brain WhatsApp sync

[Service]
Type=oneshot
ExecStart=%h/scripts/run-sb-whatsapp-sync.sh
TimeoutStartSec=600

# ~/.config/systemd/user/sb-whatsapp-sync.timer
[Unit]
Description=second-brain WhatsApp sync, after the Mac's :50 push

[Timer]
OnCalendar=*-*-* 01,07,08,09,10,11,12,13,14,15,16,17,18,19,20,21,22:55:00 Europe/Athens
RandomizedDelaySec=120

[Install]
WantedBy=timers.target

# ~/.config/systemd/user/sb-whatsapp-sync.service.d/healthcheck.conf
[Unit]
OnSuccess=hc-success@sb-whatsapp-sync.service
OnFailure=hc-fail@sb-whatsapp-sync.service
```

The Healthchecks check `sb-whatsapp-sync` must exist before the first run. Install
the Mac side first, so the first run finds a snapshot rather than exiting 66, then
`systemctl --user daemon-reload && systemctl --user enable --now sb-whatsapp-sync.timer`.

On the **Mac that runs the bridge**, after pulling this repository:

```bash
install -m 755 scripts/wrappers/launchd/sync-whatsapp-to-vps.sh scripts/wrappers/launchd/sb-db-pull.sh ~/.local/bin/
sed -e "s|__HOME__|$HOME|g" -e "s|__WHATSAPP_STORE__|/path/to/the/bridge/store/messages.db|g" \
  scripts/wrappers/launchd/com.plessas.whatsapp-sync-vps.plist \
  > ~/Library/LaunchAgents/com.plessas.whatsapp-sync-vps.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.plessas.whatsapp-sync-vps.plist
launchctl kickstart gui/$(id -u)/com.plessas.whatsapp-sync-vps   # the first push
```

**First backfill**: `systemctl --user start sb-whatsapp-sync.service` on the
producer once the first push has landed. Extraction is deadline-bounded (300 s a
run), so a large first backlog drains over a few hourly runs; starting the
service again drains it faster. Check with `python -m src.cli tool stats`
(`coverage.whatsapp`: the first and last message held, and how many) and the
nightly health report's WhatsApp line.

## 6. Schedule

The pipeline is just CLI commands, so schedule them however you like. Source your
`.env` first so the job inherits `BRAIN_DATA_DIR` and credentials.

**cron** (hourly staging and sync, a nightly attachment pass):

```cron
5 * * * *  cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python -m src.export.outlook_export --folder Inbox >> ~/.second-brain/logs/sync.log 2>&1
7 * * * *  cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python -m src.cli sync  >> ~/.second-brain/logs/sync.log 2>&1
17 2 * * * cd /path/to/repo && set -a && . ./.env && set +a && ( .venv/bin/python -m src.cli register-attachments; .venv/bin/python -m src.cli process-attachments --workers 4 --deadline-s 1800 ) >> ~/.second-brain/logs/attachments.log 2>&1
```

`sync` ends with an embedding pass, so no `embed` job is needed. The nightly pass
registers every attachment outlook-cli downloaded (the hourly sync registers 50 a
run, from the last 48 hours) and summarises what the hourly slice leaves: a text
over 50,000 characters waits for it (README, "Attachments in the hourly sync").
`--deadline-s` bounds each of its two phases.

**Linux, systemd (user) timer.** A wrapper that sources `.env` and runs the CLI:

```ini
# ~/.config/systemd/user/sb-sync.service
[Service]
Type=oneshot
WorkingDirectory=%h/plessas-second-brain
EnvironmentFile=%h/plessas-second-brain/.env
ExecStart=%h/plessas-second-brain/.venv/bin/python -m src.export.outlook_export --folder Inbox
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
`python -m src.export.outlook_export` then `python -m src.cli sync` on
`StartInterval` (or `StartCalendarInterval`). Give the labels a common prefix
(`<prefix>.attachments`, `<prefix>.auth-watch` and so on): the health check reads
it off the installed plists to find the jobs, and `BRAIN_LABEL_PREFIX` sets it
when it cannot.

`sync` stages no mail itself; `outlook_export` does, one run per folder (Inbox,
Archive, Sent Items), and `python -m src.export.inbox_reconcile` records Inbox mail
you have since moved, and where stored mail you have since deleted or junked now
is (the README's Database schema has the `mail_location:` rows it writes).
`scripts/wrappers/systemd/sb-outlook-sync.sh` runs all of these hourly.

**What `sync` does, in order** (`src/pipeline/sync.py`). Each slice is wall-clock,
checked before an item starts, and what does not fit waits for the next run:

1. Extract the staged mail: 300 s under the hourly unit or any other scheduler,
   900 s under `sb-daily-sync` and `sb-noon-catchup`, no limit at a terminal.
2. Load it, then register the attachments outlook-cli downloaded with it: at most
   50 a run, from the last 48 hours, and only when mail loaded.
3. Dedup the people the load added or changed, when mail loaded (the whole table
   once a day, or with `--full-dedup` whether or not mail loaded).
4. Attachments: Phase 1 reads text, newest mail first, for 90 s; Phase 2
   summarises, starting with what Phase 1 has just read, for 90 s, texts of up to
   50,000 characters only (README, "Attachments in the hourly sync").
5. Claude Code conversations: export the sessions of the last 7 days that changed,
   extract for 30 s, load (`--skip-conversations` leaves this out).
6. Inline images: classify and describe new ones, for 90 s.
7. One embedding pass over what the run stored, when it loaded mail or
   conversations.
8. Record `last_sync_date` and `mail_export_ok_at`, which `stats` reports.

It exits 0; 75 when extraction ended on a quota pause, or when the model extracted
none of the new mail sent to it (`last_sync_date` is then held back, so the run
cannot read as fresh); 143 on a stop signal; 1 when the database is missing; and 0
with a `skip:` line when another sync holds `state/sync.lock` (`--lock-wait` seconds
to wait for it).

**What one `sync` reads.** A run pays for what it brought, not for the whole store. The
people dedup takes the groups of the people its load added or changed and of any added
since the last pass, and the whole table once a day or with `sync --full-dedup` (or
`python -m src.store.dedup_people`); the `sync_metadata` rows `people_dedup_high_water`
and `people_dedup_full_at` hold its place. Attachment registration scans the message
directories written since its last complete scan began (`attachment_scan_started_ns`)
and those of the mail the run loaded; the nightly `register-attachments` still walks
every directory. The embedding index is saved once, at the end of the run. Conversation
staging holds one file per session and version, and a load removes what nothing can use
any more: the first load on code from 2026-10-11 or later also splits the multi-session
`conversation-batch-*.json` files, so expect it to free most of `staging/conversations`.
The export reads a transcript again only when it changed, or when the store lost what
it held (`staging/conversations/transcripts-read.json`; any change to the exporter
reads them all once).

**Conversations stay in every sync.** All three sync wrappers, the hourly
`sb-outlook-sync.sh` included, run the whole `sync`, so a session reaches the store
within the hour; `sb-conversation-sync` also exports and extracts conversations three
times a day. With staging pruned and an unchanged transcript not read again, that step is
cheap. `sync --skip-conversations` leaves it out of a run; no wrapper passes it.

Each `outlook_export` run lists its folder from six hours before its cursor, so
mail that turned visible after newer mail was listed (a scanner hold, a release
from quarantine) is still caught. It skips, before fetching, what the store or a
staging batch already holds, and a download of attachments that runs out of time
puts its message back on the retry list instead of ending the run. What the
overlap cannot reach, the nightly reconcile does:
`scripts/wrappers/systemd/sb-attachment-pass.sh` ends with
`python -m src.cli mail-reconcile --since 7d --refetch --limit 100 --health-json`.
It sets the last week of Inbox, Archive and Sent Items against the store and
stages what the store lacks for the next mail sync. `--limit` is its call budget:
three listings, plus at most two calls per message staged; set
`SB_MAIL_RECONCILE_LIMIT` in the unit's environment to change it. `--health-json`
writes `data/state/mail_loss.json`, even when the reconcile fails: `updated_at`,
`fetch_gave_up` (messages the export stopped retrying, summed over the cursor
files), `last_gave_up_at`, `quarantined` (unreadable staging batches), per-folder
counts, and the reconcile's outcome with `ok_at`, when one last succeeded. The
stage is skipped, and logged, while `~/.second-brain/needs_reauth` exists. It is
the pass's last stage and its only outlook-cli caller, so a slow night cannot
starve the stages before it.

`sync` does not cover Teams, WhatsApp, calendar, news or SharePoint, so
`teams-sync`, `whatsapp-sync`, `calendar-sync`, `news-sync` and
`process-sharepoint` each want their own schedule. `python -m src.cli --help` lists
every subcommand.

**What runs when on the reference producer**, an example of a split that works
(times are Europe/Athens; each unit runs its archived wrapper (`sb-attachments` runs
`sb-attachment-pass.sh`) through a host-local shim, see `scripts/wrappers/README.md`):

| Unit | When | What it runs |
| --- | --- | --- |
| `sb-outlook-sync` | :10 at 01 and 07 to 22 | `sb-auth-watch.sh`, the Inbox, Archive and Sent Items exports, `inbox_reconcile`, `sync`, `sweep-files --policy` |
| `sb-teams-sync` | :30 at 01 and 07 to 22 | `teams-sync` |
| `sb-whatsapp-sync` | :55 at 01 and 07 to 22 | `whatsapp-sync`, after the Mac's :50 push (section 5) |
| `sb-calendar-sync` | :48 at 07 to 22 | `calendar-sync` |
| `sb-news-sync` | :50 at even hours | `news-sync` |
| `sb-conversation-sync` | 08:45, 14:45, 20:45 | `export-conversations --days 7`, `extract-conversations`, `ingest-session-notes` |
| `sb-daily-sync` | 07:05 | `scripts/backup_db.py` (section 8), `sync` with a 900 s extraction slice, `action_lifecycle` |
| `sb-noon-catchup` | 13:17 | `sync` with a 900 s extraction slice |
| `sb-attachments` | 02:00 | `sb-attachment-pass.sh`: `register-attachments`, Phase 1 (900 s), Phase 2 (1,800 s), `process-images --limit 500`, `process-sharepoint`, the orphan reaper, `sweep-files`, `mail-reconcile --since 7d` |
| `sb-auth-watch` | every 4 hours, and before each `sb-outlook-sync` run | `sb-auth-watch.sh`: checks the Outlook and Teams sessions and renews them |
| `sb-health-check` | 23:50 | `scripts/health_check.py --fix --email-if-issues`, then the freshness ping (section 7) |
| `sb-topic-pages` | 03:30 | `topic-pages`: the live topics' pages with new evidence (below) |
| `sb-briefs` | every 15 minutes, 07:00 to 21:45, during the pilot | `briefs`: the pre-meeting brief pilot (below) |
| `sb-mcp` | always on | `python -m src.mcp_server --http` (section 9) |

The Mac that holds a replica pulls it at :45 (section 5), pushes WhatsApp at :50 if
it runs the bridge, and runs the weekly restore drill ([`RESTORE.md`](RESTORE.md)).

### Topic pages and the brief pilot

Two units beside the others, on the producer, after pulling the release that brings
them (README, "Topic pages and findings" and "Pre-meeting briefs (pilot)"). Each runs
its archived wrapper through a host-local shim, as the others do: copy
`~/scripts/run-sb-calendar-sync.sh` to `run-sb-topic-pages.sh` and `run-sb-briefs.sh`,
each exec'ing its own wrapper (the shim sources the Vertex environment both calls need).

```bash
install -m 755 scripts/wrappers/systemd/sb-topic-pages.sh scripts/wrappers/systemd/sb-briefs.sh ~/.local/bin/
python -m src.cli maint migrate                  # schema v36: topic_pages and findings, two new tables
python -m src.cli topic-pages --estimate         # the topics picked and the first build's price; no call
python -m src.cli maint topic-pages              # the first build: about $1 for 20 pages, 10-15 minutes
```

```ini
# ~/.config/systemd/user/sb-topic-pages.service
[Unit]
Description=second-brain topic pages (the live topics' pages with new evidence)

[Service]
Type=oneshot
ExecStart=%h/scripts/run-sb-topic-pages.sh
TimeoutStartSec=1800

# ~/.config/systemd/user/sb-topic-pages.timer
[Unit]
Description=second-brain topic pages, nightly after the attachment pass

[Timer]
OnCalendar=*-*-* 03:30:00 Europe/Athens
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

The job starts no page after 1,500 s (`--deadline-s`), so a slow night leaves the
rest for the next one. It exits 1 only when pages were due and every one of them
failed. A night rebuilds the pages whose evidence holds an item they were not written
from: 6 to 16 of 20 a night on the reference store, about $0.25-0.90.

The brief pilot reads its settings from one owner-only file, which the install writes
with the pilot's last day, 14 days on:

```bash
(umask 077 && mkdir -p ~/.config/second-brain && {
  echo "BRAIN_BRIEF_PILOT_UNTIL=$(date -d '+14 days' +%F)"
  echo "HEALTH_EMAIL_TO=owner@example.com"   # one of the addresses BRAIN_USER_EMAIL_PATTERN lists
} > ~/.config/second-brain/briefs.env)
python -m src.cli briefs --dry-run               # what the next pass would brief; no call, no mail
```

```ini
# ~/.config/systemd/user/sb-briefs.service
[Unit]
Description=second-brain pre-meeting brief pilot

[Service]
Type=oneshot
ExecStart=%h/scripts/run-sb-briefs.sh
TimeoutStartSec=300

# ~/.config/systemd/user/sb-briefs.timer
[Unit]
Description=second-brain pre-meeting brief pilot, on the quarter hours

[Timer]
OnCalendar=*-*-* 07..21:00/15:00 Europe/Athens
AccuracySec=1s

[Install]
WantedBy=timers.target
```

Then `systemctl --user daemon-reload && systemctl --user enable --now
sb-topic-pages.timer sb-briefs.timer`. The brief timer fires on the quarter hours,
with no randomized delay: each run briefs the meetings 30 to 45 minutes after its
quarter hour, so each meeting falls in one run's window, and the log
(`state/briefs.jsonl`) keeps a late run from briefing one twice.

The pilot stops by itself. After the date in `briefs.env` the command sends nothing
and says `pilot ended on <date>`, and the wrapper then runs `systemctl --user disable
--now sb-briefs.timer`. To extend it, move the date and enable the timer again. To
stop it at once, `BRAIN_BRIEFS_OFF=1` in the same file (the timer keeps running and
sends nothing), or disable the timer. To judge it, read the log: the meetings briefed
and what each cost, against what you did with the mails.

Nor does it close action items: extraction only appends, so run
`python -m src.store.action_lifecycle` after a successful sync. It drops
re-extracted duplicates and marks actions expired once their deadline, or
for an undated one its source, is long past. The daily wrapper in
`scripts/wrappers/systemd/sb-daily-sync.sh` does this.

## 7. Health check (optional)

`scripts/health_check.py` reports per-source freshness and job status, and can
email a summary (needs `outlook-cli` and `HEALTH_EMAIL_TO`):

```bash
HEALTH_EMAIL_TO=you@example.com python scripts/health_check.py --email
```

Other flags: `--email-if-issues` (silent on a healthy run), `--fix` (attempt the
auto-fixes), `--hc-ping` (below), and `--mcp-probe` (section 9). `--fix` starts the
loader (the daily sync) when it failed or when mail is going stale, and auth-watch
when the re-auth sentinel is set; any other failed unit is reported and left to its
own timer. The report also lists, under WRAPPER DRIFT, every archived wrapper that
differs from the copy in `~/.local/bin` (section 12).

**What it asserts beyond counts.** Besides each source's age, these rows look at the
pipeline itself. STALE in any of them fails the freshness ping, like any other STALE.

| Row | Verdict | Setting |
| --- | --- | --- |
| Emails | Each folder's newest mail against its own limit: Inbox and Archive 6 h, Sent Items 24 h, News 12 h. Triage relabels Inbox mail Archive, so the Inbox also counts its export's last success. STALE past a limit, or when an export would list from the future (its cursor ahead of the clock by more than the six-hour listing overlap). | `BRAIN_HEALTH_FOLDER_LAG_HOURS="Sent Items=48,News=12"`; 0 stops judging a folder |
| Golden query | One keyword search and two semantic ones for a fixed phrase, through the store's own search functions: `search_emails`' semantic mode and `recall`'s semantic provider, reported as `semantic` and `recall_semantic`. Any semantic status but `ok` (`keyword_seeded`, `unavailable`) is looked at again 20 s later: one that clears is a WARN, one that persists is STALE. WARN when the phrase finds nothing or no index exists. | `BRAIN_HEALTH_QUERY`, else `BRAIN_USER_ROLE`, else `BRAIN_USER_NAME` |
| Attachment lag | The oldest attachment waiting for its text, and the oldest text waiting for a summary. WARN past a day. The MCP `stats` tool returns the same counts and ages. | |
| Backups | The newest `brain-*.db` snapshot and the newest `brain-*.db.zst.enc` archive, STALE past 26 h; the last restore drill, WARN past 35 days ([`RESTORE.md`](RESTORE.md)). Not judged on a replica. | `BRAIN_BACKUP_DIR` (default `$BRAIN_DATA_DIR/backups`), `BRAIN_BACKUP_OFFSITE_DIR` (default its `offsite`), `BRAIN_RESTORE_DRILL_STAMP` |
| Mail loss | Messages the export gave up fetching, and staging batches quarantined as unreadable. WARN on any quarantined batch, and on a message given up in the last week; the cursor files keep their give-ups for good, so older ones are reported, not warned on. | Read from `state/mail_loss.json` while it is under two days old, else counted from the cursor files |

The golden query loads the whole embedding index (about 1.7 GB on the reference
producer) into the check's own process, so schedule the check where that memory is
free. On a systemd host the job list is every `sb-*` service the user manager has,
plus the bot's unit when present (section 9); the list kept in the script is the
floor, so a unit that disappears reads NOT_LOADED instead of dropping out.

**Model spend.** The `LLM spend` row prices yesterday's model calls (UTC) from the
usage log every call appends to, `llm-usage-YYYY-MM.jsonl` in the data home, with a
line per call site below the table. It warns above `BRAIN_DAILY_SPEND_WARN_USD`
(default 60, in US dollars) and reads STALE when the store shows model work that
day but the log holds no call, which fails the freshness ping like any other STALE.
To cap what one nightly attachment pass can spend, put `BRAIN_ATTACHMENT_TOKEN_BUDGET`
in the environment its unit starts with: a pass that reaches the budget leaves the
rest pending and exits 0. `BRAIN_ATTACHMENT_FULL_PARTS_UPTO` sets what a long
document costs, and `python -m src.cli process-attachments --phase 2 --estimate`
prices the pending work first; both are explained in the README's "Attachment
summaries and spend".

**Wire up the dead man's switch.** A health check that emails you is worth
exactly as much as the scheduler that runs it. If the host is off, or the timer
never fires, silence is indistinguishable from health. A
[Healthchecks](https://healthchecks.io) check alarms on the *absence* of a ping,
so a dead scheduler becomes loud instead of invisible. Both ways below ping the
check `sb-brain-freshness` under `HC_PING_URL`, the ping base URL for your
project, read from the environment. Use one of them, not both:

- `--hc-ping` on the scheduled run: `health_check.py` pings it, and pings its
  `/fail` when any data source reads STALE.
- The archived wrapper `scripts/wrappers/systemd/sb-health-check.sh`, which the
  reference producer runs: it calls the check with `--fix --email-if-issues` and
  no `--hc-ping`, reads the report's DATA SOURCES block, and pings `/fail` on a
  STALE or a FAIL there, sourcing `HC_PING_URL` from
  `~/.config/healthchecks-ping.env`.

Two rules for that variable. Treat it as a secret, because anyone holding it can
mark your checks green; this is why `health_check.py` logs curl's exit code and
never its stderr, which contains the URL. And read it from the environment only,
never as a CLI argument, so it cannot leak into a process listing or a committed
wrapper.

Ping from the scheduled run and never from an ad hoc one: a manual invocation that
pings resets the switch and hides a scheduler that has already stopped. That is why
`--hc-ping` is off by default. `scripts/backup_db.py --hc-slug <slug>` reports the
nightly backup on its own separate check, for the same reason: "the sync ran" and
"a backup was written" are different facts, and only the second one is worth a
green check on the backup.

## 8. Backup and restore

`scripts/backup_db.py` takes an MVCC-consistent snapshot of a live `brain.db`,
verifies it, and optionally writes a zstd-compressed, AES-256-encrypted offsite
copy. On the reference producer `sb-daily-sync.sh` runs it every morning with seven
local snapshots and a 14-daily, 8-weekly set of archives under the data home's
`backups/`, reporting on its own Healthchecks slug (section 7). Everything else
about backups is in [`RESTORE.md`](RESTORE.md): the artefacts and the key, the
restore and the checks that tell a good copy from a corrupt one, a file-level
backup such as restic (which takes the daily snapshot, never the live file), and
the weekly restore drill that the health check's Backups row ages (section 7).

## 9. Serve to Claude Code

Register the MCP server once (see `README.md`, "Register with Claude Code"). It
reads whichever `brain.db` sits under the local `BRAIN_DATA_DIR`.

On a single host that is the database your scheduled jobs write, so queries see
the latest ingest. On a replica it is whatever the last pull left behind: check
`~/.second-brain/db-pull.stamp`, or the `data_as_of` and `age_hours` fields the
`stats` tool returns, before trusting an answer about anything recent.

### Over HTTP: one process for every client

Each stdio client spawns its own server, and each server loads the embedding
index (about 1.6 GB) on its first semantic query. A client that cannot spawn the
server, or would spawn it per request, should use the HTTP mode instead.

```bash
# 1. A token only the owner can read
(umask 077 && mkdir -p ~/.config/second-brain && { [ -s ~/.config/second-brain/mcp-token ] || openssl rand -hex 32 > ~/.config/second-brain/mcp-token; })

# 2. The wrapper (archived at scripts/wrappers/systemd/sb-mcp.sh)
install -m 755 scripts/wrappers/systemd/sb-mcp.sh ~/.local/bin/sb-mcp.sh
```

The archived wrapper assumes the reference producer's layout (the checkout at
`~/SourceCode/plessas-second-brain`, the venv at `~/.venvs/second-brain`), so
for another layout edit `PROJECT` and `PYTHON` in `~/.local/bin/sb-mcp.sh`.

`~/scripts/run-sb-mcp.sh` is the host-local shim, like the other `run-sb-*.sh`:
it sets `PATH`, sources the Vertex environment file (query embeddings need it),
sets `BRAIN_DATA_DIR` when the host uses one (`src/config.py` reads it only from
the process environment), and execs `~/.local/bin/sb-mcp.sh`.

```ini
# ~/.config/systemd/user/sb-mcp.service
[Unit]
Description=second-brain MCP over HTTP (loopback, bearer token)
# notify-failure@ is a host-local template that mails the failure; drop the line if the host has none.
OnFailure=notify-failure@%n.service
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart=%h/scripts/run-sb-mcp.sh
Restart=always
RestartSec=10
# On systemd 254+ a crash otherwise enters failed before each restart, so OnFailure=
# would mail every 10 s. In direct mode the restarts are silent, and OnFailure= fires
# once, when the start limit in [Unit] gives up.
RestartMode=direct
# A reload after embeddings.npz changes holds one index (about 1.6 GB), plus the
# old one only while a reader is still mid-call.
MemoryMax=4G
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now sb-mcp.service
# Without the token: 401
curl --retry 5 --retry-connrefused --retry-delay 1 -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp
```

Updating the checkout restarts nothing, and sb-mcp, unlike the scheduled jobs,
keeps running the code it started with: after each update run
`systemctl --user try-restart sb-mcp.service` (section 12 has the rest of an
update).

After a start-limit failure the unit stays failed: fix the cause, then run
`systemctl --user reset-failed sb-mcp.service && systemctl --user start sb-mcp.service`.
A user service stops when your last session ends unless linger is on (section 5,
`loginctl enable-linger`).

A client authenticates with `Authorization: Bearer <token>`; for Claude Code,
an `http` entry with that header in its MCP configuration.

### Alerts for the server and the bot

A failed long-running unit raised nothing: neither sb-mcp nor the bot that reads it
had a Healthchecks check. Give each the drop-in the scheduled units carry, where
`hc-success@` and `hc-fail@` are the host-local templates that ping the check named
after their instance (`$HC_PING_URL/<name>`, and `.../fail`):

```ini
# ~/.config/systemd/user/sb-mcp.service.d/healthcheck.conf
[Unit]
OnSuccess=hc-success@sb-mcp.service
OnFailure=hc-fail@sb-mcp.service
```

```ini
# ~/.config/systemd/user/telegram-brain.service.d/healthcheck.conf (the bot's unit)
[Unit]
OnSuccess=hc-success@telegram-brain.service
OnFailure=hc-fail@telegram-brain.service
```

A long-running unit pings success only when it stops cleanly, so give these two
checks a long period: the failure ping is the signal they carry. And "active" is not
"serving", so an hourly probe asks the server for `stats` with the bearer token, the
way a client does:

```ini
# ~/.config/systemd/user/sb-mcp-probe.service
[Unit]
Description=second-brain: does the HTTP MCP server answer stats

[Service]
Type=oneshot
WorkingDirectory=%h/plessas-second-brain
EnvironmentFile=%h/.config/healthchecks-ping.env
ExecStart=%h/plessas-second-brain/.venv/bin/python scripts/health_check.py --mcp-probe --hc-ping
```

```ini
# ~/.config/systemd/user/sb-mcp-probe.timer
[Timer]
OnCalendar=hourly
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
```

It asks `http://127.0.0.1:8765/mcp` (`BRAIN_MCP_URL` to change it), reads the token
from `BRAIN_MCP_TOKEN_FILE`, else `~/.config/second-brain/mcp-token`, and never
prints it. It pings `sb-mcp-stats` (`BRAIN_MCP_PROBE_SLUG`), creating the
check on its first ping, so set that check's period to one hour. The ping fails when
the server does not answer, refuses the token, or answers on data more than 7 hours
old (`BRAIN_MCP_PROBE_MAX_AGE_HOURS`; the server's own `stale` flag turns at 3 hours,
which every night's gap in the mail timer passes). The probe exits 0 whatever it finds:
the ping carries the finding. The nightly report shows the same line under SCHEDULED
JOBS on a systemd host that has the token file.

## 10. Files are inputs

Attachment files are deleted once their content is stored; the database keeps the text,
summaries, facts and vectors. Three commands and one file control it:

| Piece | What it does |
| --- | --- |
| `python -m src.cli maint hash-attachments` | Records `attachments.sha256` for files still on disk. Run it before deletion starts: afterwards the hash is the only evidence of what a file held |
| `python -m src.cli sweep-files [--apply] [--only-newer-than ISO] [--policy]` | Deletes registered files whose content is stored. Report-only by default |
| `scripts/reap_orphan_attachments.py [--apply] [--adopt-only] [--policy]` | Unregistered directories: stored duplicates go after a day, unique files are adopted after 7 days. Unregistered files inside a registered directory: stored duplicates go, unique ones stay |
| `data/state/sweep-policy.json` | `{"apply": true or false, "only_newer_than": ISO or null}`. Absent means report-only. The wrappers pass `--policy`, so this file alone decides whether anything is deleted |

The wrappers under `scripts/wrappers/systemd/` call the sweep and the reaper; install them into
`~/.local/bin` after changing them (they are an archive, see section 12). The health check's
"Files on disk" row shows what is still held and warns when the sweep stops.

**Documents without files.** A SharePoint file linked from mail and a note a Claude session
wrote (`.md`, `.txt`, `.csv`) are stored as text only: `attachments.file_path` reads
`text:<source>:<key>`, and no file is kept. `process-sharepoint` fetches into a temporary
directory, within the nightly pass's `--deadline-s`; the conversation sync runs
`ingest-session-notes` over the transcripts new or changed since they were last read (subagent
transcripts included). Zip archives are unpacked in a temporary directory, the same file is
extracted once, and long texts are stored in full and summarised in parts. The one-time
backfills are `python -m src.cli maint sharepoint-ingest-fetched` (the files earlier fetches
left in `data/sharepoint`, which `sweep-files` then also covers), `ingest-session-notes --all`,
and `python -m src.cli maint reextract --capped --long --zip --unread`, each with a count first
(`reextract --dry-run`).

**Files are read in full.** Every sheet and row of a spreadsheet, and every page of a scan or a
multi-page TIFF, OCR'd inside a two-minute budget per file; a scan it cuts short says "pages left
unread", and `reextract` with no budget finishes it. Text stops at the 2,000,000-character
ceiling, kept after a review measured 50,000,000 (3.3 GB of memory for every write to such a row,
search snippets that never finished). A file cut there, or stopped by an archive's safety limits,
says "file kept", and the sweep keeps it for good. Phase 2 summarises a long text from some or
all of its parts, as the README's "Attachment summaries and spend" sets out; the whole text
stays stored and searchable.

Rows the old readers read in part (spreadsheets to 20 sheets of 51 rows, scans to 30 pages,
multi-page TIFFs to the first page, archives with all of these, texts at the old 100,000 storage
cap or at the 2,000,000 ceiling) count as not stored until `maint reextract --partial` reads them again:
the sweep and the reaper keep their files, and Phase 1 does not copy them to another attachment
with the same bytes. Before its first run, pull the checkout the timers run and let running `sb-*`
units finish: its first run records when it began (`sync_metadata` row
`reextract_partial_since`), and a row an older reader writes after that would be taken for a new
one. Run it in batches (`--limit 150`, as the stage 2 chain does), since Phase 2 and the vector
refresh run at the end of each batch. A re-read that changes nothing is not summarised again; one
that comes back shorter or empty keeps the stored row. Count first with
`reextract --partial --dry-run`.

**Greek read as Latin letters.** OCR reads Greek first, and a text PDF's scanned pages (a
page with under 50 characters of text that shows an image) are OCR'd inside the file's budget.
An OCR reading that is Greek in Latin look-alike letters ("kal" for «και») is stored with the
`ocr_garbled` mark in `extraction_error` and `llm_status = 'skipped'`: Phase 2 does not summarise
it and the sweep keeps its file, and keeps an original fetched back from the mailbox until it has
been read again (README, "Greek read as Latin letters"). The texts read before 2026-10-11 are
marked, fetched back and read again by four commands, in the order step 6 of section 12 gives.

**SharePoint links by kind.** A file behind a sharing or viewer link is downloaded by
`sharepoint-cli get`, which resolves the link through its viewer page; an intranet page is read
by `sharepoint-cli page` as text; a link with nothing to read is recorded `not-content` and
never fetched. A file link whose page names no file is recorded `not-a-file`: a failure the
health row shows and the retry pass offers again, since a viewer format change looks the same.
Both commands need a `sharepoint-access` build that has them (`git pull && npm ci && npm run build`
in its checkout, `~/SourceCode/sharepoint-access` unless `SHAREPOINT_CLI_PATH` points elsewhere).
The one-time backfill for links recorded `ok` with no text stored (mostly viewer pages saved as
the file) is `python -m src.cli maint sharepoint-refetch-content --max-fetches 0 --deadline-s 1800`,
with a count first (`--dry-run`). It is resumable: the `sync_metadata` row `sharepoint_refetch_since` marks when it
began, a link attempted since is done, and a finished pass finds nothing. Delete that row to read
them all again.

## 11. Batch jobs for backfills (Vertex AI batch prediction)

A backfill of attachment summaries (`reextract`, or a large Phase 2 queue) can run as Vertex AI
batch prediction jobs at half the online price (README, "Batch jobs for backfills"). The requests
go to a Cloud Storage bucket as JSONL, a job in the extraction region answers them into the same
bucket within 24 hours, and `--batch-collect` stores the replies. Nothing scheduled uses it: the
hourly sync and the nightly pass stay online.

**What to create, once, in the Vertex project.** The commands below use placeholders: `PROJECT_ID`,
`PROJECT_NUMBER`, `BUCKET` and `OWNER_EMAIL` (the account whose Application Default Credentials
the producer runs on).

1. A bucket in the jobs' region. With `VERTEX_SDK_REGION=eu` that is the `EU` multi-region; Google
   asks for the bucket and the job in the same region. Uniform access, no public access, and a
   lifecycle rule that deletes what it holds after seven days: the files carry the documents' text
   and the replies, and a job is collected within two days.

   ```bash
   printf '%s\n' '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 7}}]}' > lifecycle.json
   gcloud storage buckets create gs://BUCKET --project=PROJECT_ID --location=EU \
     --uniform-bucket-level-access --public-access-prevention --lifecycle-file=lifecycle.json
   ```

2. Roles. The producer's account creates and reads jobs and writes and reads the bucket; the
   Vertex AI service agent, which runs the job, reads the input and writes the output.

   ```bash
   gcloud projects add-iam-policy-binding PROJECT_ID \
     --member=user:OWNER_EMAIL --role=roles/aiplatform.user
   gcloud storage buckets add-iam-policy-binding gs://BUCKET \
     --member=user:OWNER_EMAIL --role=roles/storage.objectUser
   gcloud storage buckets add-iam-policy-binding gs://BUCKET \
     --member=serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com \
     --role=roles/storage.objectUser
   ```

   The account already calls Claude through `roles/aiplatform.user` or a role that holds it, so
   the first binding may be in place. The service agent's grant is the one a job's error names
   when it is missing.

3. In the producer's environment (`.env`, or the env file the units read):
   `BRAIN_BATCH_BUCKET=gs://BUCKET/second-brain`, and `BRAIN_BATCH_LOCATION` only to run the jobs
   somewhere other than `VERTEX_SDK_REGION`. The project, the model and the call shape are the
   online path's. A job's files go under `gs://BUCKET/second-brain/<job key>/`: `input.jsonl`, and
   the job's output under `output/`.

**The probe first.** On the producer, with the same environment as the timers:

```bash
python -m src.cli maint reextract --batch --probe
```

It sends ten requests built from the newest stored texts (or from the rows a selector chooses, as
in `reextract --long --batch --probe`), holds nothing, and waits up to an hour, printing the job's
name and its two paths at once. When the job ends it prints, per request, the outcome (`parsed`,
or why not), the stop reason, the tokens and the summary, then the cost at batch and at online
rates. It stores nothing; its job and its replies go in the usage log. If the wait runs out, the
job runs on, and `reextract --batch-collect` prints the replies later. What it settles:

- an error at the job's creation naming the location: `eu` takes no Claude batch job, so set
  `BRAIN_BATCH_LOCATION` to an EU region that serves the model, with a bucket in that region;
- an error naming the bucket or a permission: the roles above;
- replies counted `failed` with a message about the response's keys: the output lines are laid out
  otherwise than the code reads them, and nothing was stored. Report the message (it names the
  keys, not the text) before any backfill;
- under the default call shape, `v2-text`, run it a second time: cache reads in the usage log and
  `parsed` replies say that batch jobs take the cached system prompt and the pinned effort; an
  error naming either says to send that backfill with `BRAIN_EXTRACT_CALL_SHAPE=v1`. (`v2` adds
  structured outputs, which the reference project refuses online.)

**The backfill.** Price it first: `python -m src.cli maint reextract --long --estimate` prints the
online and the batch cost. Then, on the producer:

1. Send a job: `python -m src.cli maint reextract --long --batch --limit 500` (or `--capped`,
   `--partial`, ... as before), which reads the rows again as usual and sends their summaries.
   A job takes 2,000 requests, and a long document takes one per part it summarises (its merge
   goes in a later job), so choose `--limit` for the rows of a run to fit one job; the command
   prints how many it could not send. Pass the `highest id` it prints back as `--after-id` for the
   next run. For a Phase 2 queue: `python -m src.cli process-attachments --phase 2 --batch`,
   repeated until it says `nothing to send`. A token budget bounds what is sent as it bounds an
   online run. Vertex runs four jobs at a time in a project by default.
2. The rows a job holds are left out of the hourly sync and the nightly pass, so nothing is paid
   for twice, for 48 hours or until collected. A row the job could not take stays pending, and the
   next online pass summarises it at the online price: send it in the next job first. Sending while
   the nightly pass runs can pay for a few rows twice; the collect then finds them summarised and
   leaves them.
3. Collect, once the jobs have ended (the Vertex console, or the command, says so):
   `python -m src.cli process-attachments --batch-collect`. It prints, per job, the rows stored, the
   parts saved for a merge, and the rows left pending with the reason. A job still running is left
   for the next collect; run it again later. A `reextract` job's collect drops the vectors of the
   rows it stored, so the next index build embeds the new summaries.
   `reextract` sends one row per document (same `sha256`, or the same text when there is none) and
   its collect copies the stored summary to the other copies (`duplicates`); a long document whose
   merge goes in a later `process-attachments` job leaves its copies with their old summary.
   `--no-spreadsheets` leaves workbooks out of the rows chosen, and out of `--estimate`.
4. Repeat 1 and 3 until the selectors and the queue are empty. Long documents need one more round:
   after their parts are collected, `process-attachments --phase 2 --batch` sends their merges.
   Rows left pending (a failed line, a refusal, a reply cut short) go to the online path, which
   asks again with its refusal fallback and its larger second budget.

The usage log names each batch reply's job (`batch_job`), and the health check's spend row prices
it at batch rates.

## 12. Upgrading a producer

A `git pull` changes the code and nothing else. After one, on the producer:

1. `uv sync --frozen` when `uv.lock` changed (with `UV_PROJECT_ENVIRONMENT` set for
   a venv outside the checkout, section 2).
2. `systemctl --user try-restart sb-mcp.service`, if you run it: the HTTP server
   keeps serving the code it started with (section 9). A scheduled job starts the
   new code on its next run.
3. Install each wrapper the pull changed:
   `install -m 755 scripts/wrappers/systemd/<name>.sh ~/.local/bin/`
   (`scripts/wrappers/README.md`). Until then the timers run the old copy, and the
   nightly report lists it under WRAPPER DRIFT.
4. The schema. A new version applies when the first job that writes opens the
   store (`sync`, `teams-sync`, `whatsapp-sync`, `calendar-sync`,
   `process-sharepoint`, `load` and most one-off commands), or by hand with
   `python -m src.cli maint migrate`. Readers never migrate: `brain tool`, the MCP
   server and a replica read the file as it is, and a replica gets the new version
   with its next pull. Most migrations take well under a second and need nothing
   stopped. The few that rewrite many rows or index a large table want the timers
   stopped while they run (the notes below say which):
   `systemctl --user stop 'sb-*.timer'`, let the running `sb-*` jobs exit, pull,
   `python -m src.cli maint migrate`, then `systemctl --user start 'sb-*.timer'`.
   A host that pulls on a schedule of its own (a hook or a timer) needs that
   schedule held as well, or its next pull is migrated by whichever job starts
   first, with the timers running.
5. The one-off commands the release lists, below.

On a checkout from before 2026-10-11, which has no `maint`, drop `maint` from the
commands in this section and spell `maint calendar-backfill` as
`calendar-sync --backfill` and `maint scrub` as `scripts/scrub_secrets.py`.

### One-off commands after 2026-10-11

A store built before the 2026-10-11 release holds rows that today's code would
have stored differently. These commands bring it level, once, on the producer, in
this order, with the environment the timers run with (the Vertex settings, and
Outlook for the two that call it). Count first: `--dry-run` writes nothing, and for
`ocr-regarble`, `person_aliases` and `scrub` the dry run is the default. A store first built on
today's code needs none of them. The figures are the reference deployment's, as
examples of scale; the steps are written for any store that crosses the release.

1. **Sources and the owner.** `python -m src.cli maint classify-sources` sets every
   email's `source_class` again by today's rules and writes only the rows that
   change. The v33 migration classified the store when it upgraded; run this if
   `BRAIN_USER_EMAIL_PATTERN` did not list every address of the owner then, or has
   changed since, because the owner's addresses decide what is `automation`
   (README, "Identity"). It has no dry run.
2. **Conversations.** `python -m src.cli maint relabel-conversations --dry-run`,
   then without: the stored turns the owner did not type become `agent`. It reads
   each session's transcript where the producer still holds one, and the stored
   turn's text otherwise. A conversation left with no owner turn is an agent
   session, which every conversation read leaves out by default; nothing is
   deleted.
3. **People.** `python -m src.store.person_aliases` prints, for each mention row,
   the identity it stands for; `--apply` records them as `person_alias:` rows
   (README, "People and topics"). Optional: names resolve to identities first
   without it, and the map adds the spellings the model extracted. Delete a row to
   undo an alias. The reference store recorded 3,258.
4. **Teams**, three commands, in this order. None calls Teams or the model, and each
   can run beside the timers.
   - `python -m src.cli maint teams-backfill-replies` stores the channel replies
     that stored posts hold in their payload, through the pull's own path,
     credential redaction included; it dates each post by when it was written
     rather than by its newest reply, and sends the threads that gained replies
     back to extraction, which the next teams-sync runs work through, newest
     first, one model call each. It has no dry run, and a second run stores
     nothing. On a copy of the reference store: 3,135 replies from 1,961 posts,
     897 posts re-dated, 893 threads queued, in two seconds.
   - `python -m src.cli maint teams-strip-html --dry-run`, then without, converts
     the text of the messages stored as markup from the HTML each row keeps,
     redacting credentials as ingest does, 500 rows to a transaction; `--limit N`
     caps one run, and system messages keep their XML. Run it once more after any
     teams-sync that started before the pull has exited, since that job still
     stores markup. Until it has run, a reaction to a message stored before sends
     its thread back to extraction, because the new text differs from the stored
     markup. On a copy of the reference store: 37,741 of 79,127 messages in 5 s,
     and a search for `itemtype` went from 8,871 messages to 0. It sends nothing
     back to extraction, so the summaries, decisions, actions and facts of those
     threads, and their vectors, stay as extracted from the markup. To refresh
     them, send the threads back to extraction; the following teams-sync runs
     extract them, newest first, and embed each again (6,648 threads on that copy):

     ```sql
     UPDATE teams_threads SET extraction_status = 'pending'
     WHERE extraction_status = 'extracted' AND id IN (
       SELECT thread_id FROM teams_messages
       WHERE is_system = 0 AND content_text IS NOT content_html);
     ```

   - `python -m src.cli maint teams-drop-raw-json --dry-run`, then without, sets
     the stored payload (`teams_messages.raw_json`) to NULL, oldest first, 500
     messages to a transaction; `--limit N` stops it after N. A channel post whose
     payload still holds a reply that is not a message of its own keeps it (the
     command says how many, and to run the backfill), as does one whose payload
     does not parse. The pull still compares a message it reads again with its
     payload, so a message whose payload was dropped is written once more, the
     same text and no model call, the next time a pull reads it. It migrates
     first, since from v34 dropping a payload leaves the full-text index alone,
     and it refuses to run on a replica. About 133 MB of the 244 MB table on the
     reference producer; the file shrinks only after a `VACUUM` (step 7).
5. **Calendar.** `python -m src.cli maint calendar-backfill --dry-run`, then
   without, again until the dry run counts 0. The first calendar load kept ten
   events of each month, and `stats` names the months not held in full in
   `coverage.calendar.incomplete` (on the reference store, May to August 2025 held
   10 events each against a usual month of 228). Given no window, it fills exactly
   those, oldest first, a month at a time: it lists the month in full, fetches each
   event it does not hold (one get-event call each, and a model call where an
   invitation has text beyond its joining details, once per series), marks
   cancelled what Outlook no longer lists, and records the month in
   `sync_metadata`. A run that stops leaves its month flagged `partial`, and the
   next run resumes there. Each month's listing is bounded at 250 list-calendar
   calls. It lists only months already over, so it can run beside the hourly
   timer. To go back before the first month held, give the window:
   `--since 2025-03-01 --until 2025-05-01`.

   Invitations are now read as invitations, but the decisions and actions taken
   from them before stay in the store until you clear them: on the reference store,
   477 of 519 calendar decisions and 253 of 367 calendar actions sat on occurrences
   of a series or on single meetings extracted before they took place. No command
   clears them; to do it:

   ```sql
   DELETE FROM decisions WHERE event_id IN (SELECT id FROM calendar_events
     WHERE recurrence_master_id IS NOT NULL OR body_extracted_at < start_at);
   DELETE FROM action_items WHERE event_id IN (SELECT id FROM calendar_events
     WHERE recurrence_master_id IS NOT NULL OR body_extracted_at < start_at);
   ```

6. **Greek read as Latin letters** (README, "Greek read as Latin letters", has what
   each command does), in this order:
   1. `python -m src.cli maint ocr-regarble` counts the stored OCR texts the
      detector flags and lists their attachment ids, on disk or swept;
      `--apply` marks them and clears their summaries, decisions, actions, facts
      and vectors.
   2. `python -m src.cli maint refetch-garbled --dry-run`, then `--limit 200`,
      repeated until both its `to try` lines read 0, fetches the swept originals
      back from the mailbox: by id, then by folder, time and subject. Exit 4 means
      `outlook-cli` needs a login: log in and run it again. Mail over a year old is
      gone from the mailbox and recorded as such.
   3. `python -m src.cli maint reextract --garbled --dry-run`, then without, reads
      the marked scans on disk again, Greek first, and summarises the ones that now
      read.
   4. The nightly image pass, or `python -m src.cli maint transcribe-images --limit 0`
      at once, transcribes the marked content images; the transcription replaces
      the garbled text.

   `ocr-regarble --apply` changes only what it has not changed before: run it again
   after a large `reextract` pass, since a re-read that newly marks a row
   summarised earlier leaves that summary for it to clear.
7. **Credentials stored before masking.** `python -m src.cli maint scrub` counts
   the credentials, card numbers, IBANs and passwords stored before ingest masked
   them; with the jobs that write the store stopped, `--apply` masks them as ingest
   does now, and `--apply --vacuum` also gives back the space the steps above
   freed, given about twice the database's size free on disk (README, "Security").
8. **Batch jobs, only to use them.** The bucket, the roles and `BRAIN_BATCH_BUCKET`
   of section 11, then `python -m src.cli maint reextract --batch --probe` before
   any backfill.

On the reference deployment, `relabel-conversations`, `person_aliases --apply`,
`teams-backfill-replies`, `teams-strip-html`, the calendar backfill,
`ocr-regarble --apply` and `refetch-garbled` ran with the release on 2026-10-11, so
their figures above are what they did there, not work still pending there;
`teams-drop-raw-json` and the batch probe had not run.

### One-off commands after 2026-10-16 (attachments and ingest)

No schema change. Each runs on the producer, beside the timers, once the release's code
is there; the counts are the reference replica's on 2026-10-16, as examples of scale.

1. **Stamps in UTC.** `python -m src.cli maint utc-stamps` counts the attachment and
   sync stamps stored in local time with no offset (README, "Attachment stamps");
   `--apply` converts them, 500 rows to a transaction. Nothing waits on it, since every
   reader compares instants meanwhile; it brings the store level so that plain SQL reads
   them right too. Each stamp sits behind its row's text, so the run reads every stored
   text once (minutes on a 3 GB store) and writes one page a row. Run it once more after
   every job that started before the pull has exited, since those still write local
   time. 51,907 content rows and `last_sync_date` on the reference replica.
2. **SharePoint shells.** `python -m src.cli maint sharepoint-shells` counts the hollow
   documents an old fetch left (README, "SharePoint"), by what their links say;
   `--apply` forgets the ones whose content another document holds, the ones linked to
   nothing readable and the ones no link names, and queues for the retry pass the links
   whose content is not held. It fetches nothing and calls no model: `brain
   process-sharepoint`, or the nightly pass, reads the queued links, and a second
   `sharepoint-shells --apply` after that forgets the shells they now hold. Reference
   replica: 883 held, 181 linked to nothing readable, 38 named by no link, 4 links to
   queue, of 1,106.
3. **Calendar text.** `python -m src.cli maint calendar-bodies --dry-run` counts the
   stored events with no text (README, "Calendar"); `--limit 500`, repeated until the
   dry run counts only the events Outlook no longer returns, fetches each one's text,
   one `outlook-cli get-event` call each, newest first, and stores that column alone:
   no model call. Exit 4 means `outlook-cli` needs a login: log in and run it again.
   3,701 events on the reference producer.
4. **Wide workbooks.** `python -m src.cli maint reextract --wide --estimate` prices the
   re-summary of the long workbooks summarised from a digest that left out their newest
   columns (README, "Attachment summaries and spend"); `python -m src.cli maint reextract
   --wide` summarises them again from the new digest, from the stored text (`--batch`
   at half the price). Reference replica: 8 rows, 7 of them distinct, 7 calls, about
   77,000 input tokens, $0.28 online, $0.14 as a batch job. The first run records when
   it began, so a second takes only what is left.
5. **OCR orientation.** Nothing to run. Each image and scan page is turned upright
   before it is read (README, "Scans on their side or upside down"), which needs
   Tesseract's orientation data: `tesseract --list-langs | grep -x osd` (Debian's
   `tesseract-ocr` package ships it). A host without it reads every page as it is, as
   before. Marked scans read again by `reextract --garbled`, and scans read again by
   `reextract --ocr`, are turned upright too.

### One-off commands after 2026-10-16 (call shape v2-text, schema v35)

This release makes call shape `v2-text` the default for email and attachment
extraction (README, "Extraction call shape"), stores what schema v2 says about each
item (v35, below), takes no decisions, actions or commitments from news or automation
mail, and widens the automation stamp to written dates. On the producer, in this order,
with the environment the timers run with:

1. **Before the pull, choose the shape.** The default changes with the code, at the
   next hourly sync after the pull. To keep v1 until step 2 has run, put
   `BRAIN_EXTRACT_CALL_SHAPE=v1` in the timers' environment before pulling, and take
   it out after. A Vertex batch job sent before the pull was sent in v1 and is
   collected only under v1: `BRAIN_EXTRACT_CALL_SHAPE=v1 python -m src.cli
   process-attachments --phase 2 --batch-collect`. A batch backfill under v2-text
   wants `maint reextract --batch --probe` first: the batch service is not yet known
   to take the pinned effort and the cache marker.
2. **The eval.** `python scripts/extraction_eval.py --dry-run --synthetic --call-shape
   v2-text` costs nothing: it grades the gold and prints the request check, which
   must read `0 with output_config.format`. Then
   `python scripts/extraction_eval.py --run --synthetic --judge --yes --call-shape v2-text`
   and the same with `--call-shape v1`, and compare the two records in
   `results.jsonl` (`schema_valid`, `must_not`, `evidence_verbatim`, `owner_roles`, the
   judge's F1). The estimate prints first: on the replica's prompts, 12 extraction
   calls at most $0.47 under v2-text and $0.25 under v1, and 12 judge calls at most
   $0.25 each run, upper bounds. With the owner's labelled set, run it the same way
   without `--synthetic` (about $3 a shape).
3. **Sources and the owner.** List every address the owner sends from in
   `BRAIN_USER_EMAIL_PATTERN`, then `python -m src.cli maint classify-sources`. With
   the work addresses listed it turns 736 stored reports from `mail` to `automation`
   on the reference replica; reports sent from an address the pattern does not list
   stay `mail`.
4. **Items from news and automation.** `python -m src.cli maint forget-itemless`
   counts, `--apply` deletes, in one transaction. On the reference replica, before
   step 3: 44,627 decisions (19,606 news, 25,021 automation), 23,194 actions, 5,113
   commitments; step 3 adds the reports it reclassified. No model call.
5. **Boilerplate facts.** `python -m src.cli maint mark-boilerplate` counts the fact
   texts found in five or more distinct threads and prints the commonest; `--apply`
   marks them and records their keys, so later copies are marked as they load. On the
   reference replica: 82,412 of 1,207,372 key facts, 5,399 texts, counted in 13 s;
   nothing is deleted. It reads every key fact once into temporary tables; no model
   call. Run it before step 6, so the facts step 6 writes are marked as they load,
   and again now and then: the loader marks only texts already known.
6. **Stacked attachment facts.** `python -m src.cli maint reextract-stacked` prices
   it and changes nothing: 6,328 emails on the reference replica, one call each,
   $157 at most with 1,500 output tokens a call, about $115 at 900, the system prompt
   cached. Then once a day,
   `python -m src.cli maint reextract-stacked --apply --token-budget 20000000 --limit 2500`
   (about $45, which with the hourly jobs' $10-15 a day stays under the $60 alert),
   until the dry run counts 0: three days. It resumes where it stopped; exit 75 means
   the service stopped it, and the next run carries on. It runs beside the timers,
   one commit per email.

### After 2026-10-16 (retrieval ranking)

No schema change and no one-off command: the ranking is in the query code alone (README,
"Newest first, one row a report", "Names in either script", "Unified recall").

1. `systemctl --user try-restart sb-mcp.service` (step 2 above): the HTTP server ranks
   as it did until it restarts. A Mac's MCP servers take the new code with each new
   Claude Code session.
2. Nothing to set. To go back for one deployment, put these in the searching process's
   environment (`$BRAIN_DATA_DIR/env` or `~/.second-brain/env` for the MCP server):
   `BRAIN_QUERY_RANKING=relevance` (relevance alone, no editions, recall's email bucket
   alone fused with semantic search), `BRAIN_QUERY_NAMES=0` (no Latin and Greek name
   forms), `BRAIN_RECALL_WORKERS=1` (recall's buckets one after another).
3. `recall` opens up to six more read-only connections per call, one per bucket at a
   time, closed when it returns; they share the process's one copy of the vector index.
4. To compare, `scripts/retrieval_eval.py --run-qa` grades the plain modes as the code
   before ranked and the `_recency` modes with it; on the reference replica's 45
   questions, recall@10 0.62 to 0.78 in `recall`, 0.80 to 0.85 in `hybrid`.

### One-off commands after 2026-10-16 (topic pages, findings, brief pilot, schema v36)

On the producer, with the environment the timers run with:

1. **The schema.** `python -m src.cli maint migrate`, or the first job that writes:
   v36 adds two tables (below); nothing needs stopping.
2. **The units.** Section 6, "Topic pages and the brief pilot": install the two
   wrappers and shims, price and run the first build, write `briefs.env`, enable
   the two timers.
3. **Topics to name or skip.** `python -m src.cli topic-pages --estimate` prints the
   topics the rule picks. Put a topic whose tags fragment in `BRAIN_TOPIC_PAGES`
   (`"budget 2027|business plan 2027"` makes one page of both), and a tag the rule
   finds live in a busy month but that is no topic (a unit's name) in
   `BRAIN_TOPIC_PAGES_SKIP`, both in the topic-pages shim's environment; then
   `python -m src.cli maint topic-pages` for the pages that changed.
4. **Findings.** None to backfill: no note held a findings block before this
   release. Research teammates write them from now on (README, "Topic pages and
   findings"); `ingest-session-notes`, in `sb-conversation-sync`, loads them three
   times a day, each checked against its source. `python -m src.cli findings` lists
   the rejected ones with why, for review (read-only, so a replica runs it too).

### One-off commands after the numbers registry (schema v37)

The figures registry is new tables only (README, "Figures"). The hourly sync reads the
figures of each new edition of a report series from then on; the editions already held
wait for the backfill, which runs on the producer beside the timers, once:

1. `python -m src.cli maint figures-backfill --estimate` counts the spreadsheet editions,
   the report series they make and the editions waiting, and parses a sample of 100 to
   project the figures and the time, storing nothing. It only reads, so it runs on a
   replica too. Reference replica: 4,548 editions in 185 series, about 565,000 figures,
   projected at 1.7 minutes on a Mac.
2. `python -m src.cli maint figures-backfill --apply` reads them all, oldest first within
   each series, and commits each edition, so a run stopped part way (`--limit N`, a
   SIGTERM, a reboot) resumes where it stopped when run again. No model call. A copy of
   the reference replica's corpus took 1.5 to 4 minutes on a Mac and 40.4 MB;
   allow a few times that on a small VPS. Run it once more after every job that started
   before the pull has exited, for the editions those registered without reading.
3. After a change of the parser (`PARSER_VERSION` in `src/extract/figures_parse.py`),
   `python -m src.cli maint figures-backfill --apply --reset` forgets every figure and
   reads them all again.

### Schema notes

A store that missed several releases takes every migration since its version in
one pass, at the first job that writes. The versions that ask for a step:

- **v23.** An HTML body is stored as the text a reader sees, the HTML kept
  compressed in `email_html`. Emails loaded before keep their HTML body until
  `python -m src.cli maint split-html` converts them, on the producer. It can run
  beside the timers (`--dry-run` is read-only), redacts credentials as ingest does,
  zeroes the pages it frees and ends by optimizing the full-text index. Run it once
  more after every timer that started before the deploy has exited. On a 3.5 GB
  store it converted 19,780 bodies in 131 s; the file shrinks only after a
  `VACUUM` (`maint scrub --apply --vacuum` does both jobs in one quiet window).
- **v24.** A Teams call record, and a notice that a call was recorded or
  transcribed, is a system message. The migration marks the stored ones and sends
  back to extraction the threads whose other messages clear extraction's floor (on
  a copy: 1,261 rows marked, 95 threads queued, in 0.2 s). Stop
  `sb-teams-sync.timer`, and let a running sync exit, before the pull: a sync still
  running the old code stores call records unmarked after the migration has run.
- **v25.** Calendar `start_at` and `end_at` carry a trailing `Z` (UTC). Stop
  `sb-calendar-sync.timer` before the pull, for the same reason. The months the old
  listing capped are step 5 above.
- **v26.** The WhatsApp tables (section 5).
- **v32.** Three indexes on `attachment_content` let the file sweep and two
  health-check queries skip the stored text (the sweep's query went from 14 s cold
  to 0.3 s on a copy). Each is built by reading the table once, minutes on a large
  store with a cold cache, with the write lock held: the migration waits up to
  thirty minutes for it, the other units sixty seconds. Stop the `sb-*` timers, let
  the running syncs exit, pull, `maint migrate`, start them again. Until it has
  run, the sweep works as before, only slower.
- **v33.** `emails.source_class` (README, "News and automation are left out by
  default"); the migration classifies the stored rows. Step 1 above when the
  owner's addresses were not all listed.
- **v34.** Each full-text index's update trigger fires only when an UPDATE sets the
  row's id or a column the index reads, and two indexes on `people` serve the
  address and name lookups (0.013 s on a synthetic 530 MB store; no index is
  rebuilt). Nothing needs stopping: a job still running the old code works with the
  new triggers. The same release records the embedding space in `embeddings.npz` on
  its next save (README, "Database schema", Vector index), with nothing to run.
  After a deliberate change of model, task type, dimension or text template, start
  the index over with `python -m src.cli embed --force` on the producer, apart from
  the embedding jobs (mind the API quota); `teams-sync` and `whatsapp-sync` add
  their vectors back, 500 a run.
- **v35.** The item-state columns on `decisions`, `action_items`, `commitments` and
  `key_facts`, and `boilerplate_fact_keys` (README, "Database schema"). ADD COLUMN
  only: no row is rewritten and no full-text index rebuilt, so it takes well under a
  second and nothing needs stopping. A job still running the old code writes NULL in
  the new columns, as v1 rows hold. The one-off commands of 2026-10-16 above fill
  what they can.
- **v36.** `topic_pages` and `findings` (README, "Topic pages and findings"). Two new
  tables and three indexes, no row rewritten and no full-text index touched, so
  nothing needs stopping.
- **v37.** The numbers registry: `figure_series`, `figure_editions`, `figure_metrics`,
  `figure_values`, `figure_restated`, the view `figure_metrics_search` and the index
  `figure_metrics_fts` (README, "Figures"). New tables only, created in well under a
  second; nothing needs stopping. The registry fills from the backfill above. A replica
  that runs this code on a store pulled before the migration answers `figures` with a
  `reason` saying the registry is not built there yet.

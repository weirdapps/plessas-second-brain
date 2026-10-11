# Deployment

The engine runs from this repo plus your own environment, no external private
repo is required. It works on macOS (launchd) and Linux (systemd/cron), either
on a single host or split across two (see "Topology" below).

## 1. Prerequisites

- Python 3.12+, and [`uv`](https://docs.astral.sh/uv/), which installs from the
  committed `uv.lock`.
- `sqlite3` on `PATH`. `scripts/health_check.py` and the replica pull both shell
  out to it.
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
`pip install`. `--no-build` refuses to run a package's `setup.py`, as CI does. (`pip install -e ".[dev]"` into a Python 3.12+ venv of your own
also works.)

The `dev` extra installs `pytest` and `pytest-cov` only. `ruff` is deliberately
not in it because CI pins an exact version; run it with
`uvx ruff@0.16.8 check .` (see `README.md`, "Lint and format").

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
no Vertex project is set. See `.env.example` for the full list.

`BRAIN_USER_EMAIL_PATTERN` lists every address the owner sends from, primary and
aliases, separated by commas: `BRAIN_USER_EMAIL_PATTERN=first.last@example.com,alias@example.com`.
Each is matched whole and case-blind, so an alias that is not listed is not the
owner's. After changing it, run `python -m src.cli classify-sources` on the
producer; the next calendar sync refreshes the meetings' self flags.

Nothing in the code reads `.env`: the scheduling recipes in section 6 source it
(`set -a; . ./.env`, `EnvironmentFile=`). The MCP server inherits the
environment `claude` was launched with, which sources no `.env` and, from a GUI
or an IDE, no shell profile, so put the identity settings
(`BRAIN_USER_NAME`, `BRAIN_USER_ROLE`, `BRAIN_USER_EMAIL_PATTERN`,
`SHAREPOINT_HOST`) in `~/.config/second-brain/env` too; `src/config.py` reads
that file at import, and ignores any other key in it.

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
python -m src.cli load    # creates brain.db, then runs every migration
python -m src.cli stats   # opens it; every count is 0 on a fresh host
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
  (copying a WAL-mode database in place can capture a torn read) into
  `brain.db.new`, runs `PRAGMA quick_check` on that, and only then renames it
  over `brain.db`. `embeddings.npz` follows the same way, checked with
  `unzip -t`, and only beside a database that passed. A copy that fails its
  check is discarded and the replica stays as it was; the run logs the
  `ERROR`, exits 1 and leaves the stamp alone. On success it writes a UTC
  timestamp to `~/.second-brain/db-pull.stamp`.

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

Conversations stored before this change hold every user-role turn as the
owner's. Relabel them once, on the producer:

```bash
python -m src.cli relabel-conversations --dry-run   # counts only, writes nothing
python -m src.cli relabel-conversations
```

It reads each session's transcript where the producer still holds one, and the
stored turn's text otherwise. A conversation left with no owner turn is an agent
session, which every conversation read leaves out by default; nothing is deleted.

## 6. Schedule

The pipeline is just CLI commands, so schedule them however you like. Source your
`.env` first so the job inherits `BRAIN_DATA_DIR` and credentials.

**cron** (hourly staging and sync, daily embed):

```cron
5 * * * *  cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python -m src.export.outlook_export --folder Inbox >> ~/.second-brain/logs/sync.log 2>&1
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
`StartInterval` (or `StartCalendarInterval`). Set `BRAIN_LABEL_PREFIX` to
namespace the labels.

`sync` covers staged mail: extraction, load, attachments, people dedup,
embeddings, Claude Code conversations and inline images. It stages no mail
itself; `outlook_export` does, one run per folder (Inbox, Archive, Sent Items),
and `python -m src.export.inbox_reconcile` records Inbox mail you have since
moved, and where stored mail you have since deleted or junked now is (the
README's Database schema has the `mail_location:` rows it writes).
`scripts/wrappers/systemd/sb-outlook-sync.sh` runs all of these hourly.

**What one `sync` reads.** A run pays for what it brought, not for the whole store. The
people dedup takes the groups of the people its load added or changed and of any added
since the last pass, and the whole table once a day or with `sync --full-dedup` (or
`python -m src.store.dedup_people`); the `sync_metadata` rows `people_dedup_high_water`
and `people_dedup_full_at` hold its place. Attachment registration scans the message
directories written since its last complete scan began (`attachment_scan_started_ns`)
and those of the mail the run loaded; the nightly `register-attachments` still walks
every directory. The embedding index is saved once, at the end of the run. Conversation
staging holds one file per session and version, and a load removes what nothing can use
any more: the first load after this change also splits the multi-session
`conversation-batch-*.json` files, so expect it to free most of `staging/conversations`.
The export reads a transcript again only when it changed, or when the store lost what
it held (`staging/conversations/transcripts-read.json`; any change to the exporter
reads them all once).

**Conversations stay in every sync.** All three sync wrappers, the hourly
`sb-outlook-sync.sh` included, run the whole `sync`, so a session reaches the store
within the hour; `sb-conversation-sync` also exports and extracts conversations three
times a day. With staging pruned and an unchanged transcript not read again, that step is
cheap. `sync --skip-conversations` leaves it out of a run; no wrapper passes it. No
wrapper or unit changes with this, and nothing to migrate.

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
starve the stages before it. Copy both wrappers into `~/.local/bin` after
pulling this change.
It does not cover Teams, calendar,
news or SharePoint, so `calendar-sync`, `teams-sync`, `news-sync` and
`process-sharepoint` each want their own schedule. `python -m src.cli --help` lists every subcommand.

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
auto-fixes), `--hc-ping`, and `--mcp-probe` (section 9). `--fix` restarts only the
loader (the daily sync) and queues auth-watch for a re-auth sentinel; any other failed
unit is reported and left to its own timer.

**What it asserts beyond counts.** Besides each source's age, these rows look at the
pipeline itself. STALE in any of them fails the freshness ping, like any other STALE.

| Row | Verdict | Setting |
| --- | --- | --- |
| Emails | Each folder's newest mail against its own limit: Inbox and Archive 6 h, Sent Items 24 h, News 12 h. Triage relabels Inbox mail Archive, so the Inbox also counts its export's last success. STALE past a limit, or when an export would list from the future (its cursor ahead of the clock by more than the six-hour listing overlap). | `BRAIN_HEALTH_FOLDER_LAG_HOURS="Sent Items=48,News=12"`; 0 stops judging a folder |
| Golden query | One keyword and one semantic search for a fixed phrase, through the store's own search functions. STALE when the semantic half falls back to keyword matches or cannot run, WARN when the phrase finds nothing. | `BRAIN_HEALTH_QUERY`, else `BRAIN_USER_ROLE`, else `BRAIN_USER_NAME` |
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
rest pending and exits 0. `python -m src.cli process-attachments --phase 2 --estimate`
prices the pending work first. The README's "Attachment summaries and spend" has the
details.

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

**Upgrading to schema v23.** From v23 an HTML body is stored as the text a
reader sees, each link's address after its text (bar an image's, and a long
query string), with the HTML kept compressed in `email_html`. Emails loaded
before it keep their HTML body until `python -m src.cli split-html` converts
them, on the producer. It reads and converts a batch with no lock held and
writes it in one short transaction, so it can run beside the timers; it redacts
credentials on the way, as the ingest path does, and zeroes the pages it frees,
so no copy of a redacted key is left for a pull or a snapshot to take; and it
ends by optimizing the full-text index. `--dry-run` opens the database
read-only, so it changes nothing, the schema included. Run it once more after
every timer that started before the deploy has exited (or the next day): a job
still running the old code loads HTML bodies as before, and a re-run converts
only those. On a copy of a 3.5 GB store it converted 19,780 bodies (964 MB of
HTML to 155 MB of text, the HTML kept in 123 MB) in 131 s, and the body index
went from 425 to 226 MB. The file shrinks only after a `VACUUM`, which needs the
writers stopped and about twice the database free: 3.5 GB became 2.6 GB.
`scripts/scrub_secrets.py --apply --vacuum` does both jobs in one quiet window,
and scrubs the kept HTML as well.

**Upgrading to schema v24.** From v24 a Teams call record, and a notice that a
call was recorded or transcribed, is a system message, as the service writes it;
ingest took them for messages, counted them as their sender's, and put their XML
into the threads the model reads. The migration marks the ones stored, counts
again without them each thread that also holds other messages, and sends back to
extraction those whose remaining messages clear extraction's floor, which the
next teams-sync runs, one model call and one new vector each. On a copy of the
replica on 2026-09-24: 1,261 rows marked, 123 threads counted again, 95 queued,
in 0.2 s. A thread of calls alone, or with too little else, keeps its summary. Stop
`sb-teams-sync.timer`, and let a running sync exit, before the pull: a sync still
running the old code stores call records unmarked, and the migration runs once.
Then run `python -m src.cli migrate` and start the timer again.

**Upgrading to schema v25.** From v25 calendar `start_at` and `end_at` carry a
trailing `Z`. Graph returns them in UTC, and without the zone a 16:00 Athens
meeting read as 13:00. The migration appends it to the stored rows, about 1,100,
in milliseconds, and changes nothing else. Stop `sb-calendar-sync.timer`, and let
a running sync exit, before the pull: a sync still running the old code writes
bare times after the migration has run. Then run `python -m src.cli migrate` and
start the timer again. The same release lists every event of the window, where
the old listing kept the ten earliest of each month, so run
`python -m src.cli calendar-sync --backfill` once to fill the months it capped,
best after outlook-access pages list-calendar itself.

**Channel replies.** teams-sync stores each channel post's replies as messages of the
post's thread; until then they stayed inside the post's stored payload, out of search and
extraction. It also brings up to date a message edited or deleted after it was stored. To
store the replies already held, run `python -m src.cli teams-backfill-replies` once on the
producer after the pull. It calls no Teams API: it reads the stored payloads and stores the
replies through the pull's own path, credential redaction included. It also dates each post by
when it was written rather than by its newest reply, and sends the threads that gained replies
back to extraction. The next teams-sync runs extract them, newest first, one model call each,
within their deadline. A second run stores nothing. On a copy of the replica's Teams tables
on 2026-10-11: 3,135 replies from 1,961 posts, 897 posts re-dated, 893 threads queued, in
two seconds. No schema change, and it can run beside the timers.

**Teams message text.** teams-sync stores a message's text as a reader sees it whenever its
content holds markup, whatever type the payload declares. It used to wait for a declared
"html", which the chat service never sends, so the text of every HTML message was its markup.
To convert the rows stored before, run `python -m src.cli teams-strip-html` once on the
producer after the pull (`--dry-run` counts first, read-only). It calls neither Teams nor a
model: it converts each row from the HTML the row keeps, redacting credentials as ingest
does, writes only the rows whose text changes, 500 to a transaction, so it can run beside
the timers, and the full-text index follows through its trigger. System messages keep their
XML. A second run changes nothing, and `--limit N` caps the rows one run changes. Run it once
more after a teams-sync that started before the pull has exited, since that job stores
markup as before. Until it has run, a reaction to a message stored before sends its thread
back to extraction, as an edit does, because the new text differs from the stored markup.
On a copy of the replica's Teams tables on 2026-10-11: 37,741 of 79,127 messages converted
in 5 s, a second run changed none, and a search for `itemtype` went from 8,871 messages to
0. No schema change.

It sends nothing back to extraction, which is where the cost is: the summaries, titles,
decisions, actions and facts of the threads concerned stay as extracted from the markup, and
their vectors, which embed the summaries, with them. To refresh them, send those threads back
to extraction; the following teams-sync runs extract them, newest first, one model call each
within their deadline, and embed each again, since it was extracted after it was embedded
(6,648 threads on that copy):

```sql
UPDATE teams_threads SET extraction_status = 'pending'
WHERE extraction_status = 'extracted' AND id IN (
  SELECT thread_id FROM teams_messages
  WHERE is_system = 0 AND content_text IS NOT content_html);
```

**Teams raw payloads.** Each Teams message also keeps the payload teams-cli returned
(`teams_messages.raw_json`): 133 MB of the 244 MB table on the producer. The pull compares a
message it reads again with it to tell an edit or a delete, so new messages keep writing it, and
`teams-backfill-replies` reads the replies a post's payload holds. Nothing else reads it. Run
`python -m src.cli teams-drop-raw-json` once on the producer, after `teams-backfill-replies` and
`teams-strip-html`, with `--dry-run` first to see the count and the size. It sets the payload to
NULL oldest first, 500 messages per short transaction, so it can run beside the timers, and
`--limit N` stops it after N. A channel post whose payload still holds a reply that is not a
message of its own keeps its payload (the command says how many and to run the backfill), as
does one whose payload does not parse. It migrates first: from v34 dropping a payload leaves
the full-text index alone. A message whose payload was dropped gets it back the next time a
pull reads it, one rewrite of the same text with no model call. The file shrinks only after a
`VACUUM`. It refuses to run on a replica.

**Upgrading to schema v32.** From v32 `attachment_content` has three small indexes that
answer the file sweep's classification and two health-check queries without reading the
stored text. `extracted_text` (3 GB on the producer) sits ahead of the columns those queries
read, and SQLite reaches a column behind a long text by reading all of it: the sweep took
160 s with a cold cache inside every hourly sync, and the nightly check 8.5 minutes against a
300 s unit limit. On a copy of the replica on 2026-10-08 the sweep's query went from 14 s
cold (3.8 s warm) to 0.3 s and the other two from 0.5 s to under 10 ms; the three indexes
take 6.5 MiB. Each is built by reading the table once, so on the producer with a cold cache
expect a few minutes each, with the write lock held meanwhile (the migration waits up to
thirty minutes for it, the other units only sixty seconds). Stop the `sb-*` timers, let the
running syncs exit, pull, run `python -m src.cli migrate`, and start the timers again. A
store that has not migrated yet sweeps as before, only slower: the index hint is given only
when the index exists.

**Upgrading to schema v34.** From v34 each full-text index's update trigger fires only when an
UPDATE sets the row's id or a column the index reads; before, any UPDATE re-indexed the row's
whole text, so a mailbox move re-tokenised a body and every `llm_status` write of an attachment
up to 2M characters. Address lookups (`LOWER(email) = ?`, for every sender, recipient and cc
loaded) and the loader's name lookups get an index each on `people`, and keep their meaning.
The migration recreates ten triggers and builds two indexes on a table of 34K rows: on a
synthetic 530 MB store with 60K emails and 34K people it took 0.013 s, and afterwards 2,000
mailbox moves took 0.08 s instead of 0.66 s and 2,000 address lookups 0.004 s instead of 1.8 s.
It reads no row of the indexed tables and rebuilds no index. Any command that migrates applies
it (`sync`, `teams-sync`, `migrate`), so no timer needs stopping: a job still running the old
code works with the new triggers, and its own migrations end at v33.

The same release records in `embeddings.npz` the embedding space its vectors are in (model,
dimension, task type, text template): see Vector index in the README's Database schema. Nothing
to run: an index without it is taken as the current space and tagged on its next save, by
`sync`, `teams-sync` or `whatsapp-sync`. A job still running the old code saves the index
without it again, and the next save puts it back. From then on a writer refuses vectors of
another space, and a search that would embed its query into another space ranks around its
keyword matches instead and says why on stderr, on the producer and on a replica alike. After a
deliberate change of model, task type, dimension or text template, start the index over with
`python -m src.cli embed --force` on the producer, apart from the embedding jobs: it embeds
every email, attachment and conversation summary again (mind the API quota), and the next
`teams-sync` and `whatsapp-sync` add their vectors back.

**Calendar months the first load capped.** The first calendar load kept ten events of each
month, and the `--backfill` run after v25 listed the 365 days before it ran, which began after
them: on the replica on 2026-10-11, May to August 2025 held 10 events each and September 2025
held 34, against a usual month of 228. `stats` now counts the calendar per month and names such
months in `coverage.calendar.incomplete`, and `calendar-sync --backfill`, given no window, fills
exactly those, oldest first, a month at a time: it lists the month in full, fetches each event
it does not hold (one get-event call each, and a model call where an invitation has text beyond
its joining details, once per series), marks cancelled what Outlook no longer lists, and
records the month in `sync_metadata`. A run that stops leaves its month flagged `partial`, and
the next run resumes there; a recorded month is never listed again unless one of its events
still owes an extraction, and coverage stops flagging it. No schema change. Once, on the
producer after the pull, with calendar-sync's environment loaded (Outlook and the model):

```bash
python -m src.cli calendar-sync --backfill --dry-run   # lists the flagged months; counts what it would fetch; writes nothing
python -m src.cli calendar-sync --backfill             # fills them; run it again until the dry run counts 0
```

Each month's listing is bounded at 250 list-calendar calls, and a busy month takes a few dozen
at most, one when outlook-access pages list-calendar itself. It can run beside the timer: it
lists only months already over and leaves the hourly unit's heartbeat and fetch-failure counts
alone. To go back before the first month held, give the window, for example
`--backfill --since 2025-03-01 --until 2025-05-01`; a window is walked the same way, and the
months a backfill recorded are skipped.

From the same release a recurring occurrence carries `is_recurring` (it was 0 on every row:
the field read was one Outlook never sends) and its series in `recurrence_master_id`; every
calendar-sync sets the flag on the occurrences already stored, about 2,000 rows the first
time. And an invitation is read as one: a meeting still to come, and every occurrence of a
series, gets its purpose, agenda and who is expected, and no decisions or actions, while only
a single meeting already over is asked for decisions, from its notes. An event is extracted
again under the new prompt the next time it changes, and an occurrence whose series'
invitation was already read takes that reading without a model call. The decisions and actions
extracted from invitations before stay in the store until you clear them: on the replica on
2026-10-11, 477 of the 519 calendar decisions and 253 of the 367 calendar actions (66 still
open) sit on occurrences of a series or on single meetings extracted before they took place.
No command clears them; to do it on the producer:

```sql
DELETE FROM decisions WHERE event_id IN (SELECT id FROM calendar_events
  WHERE recurrence_master_id IS NOT NULL OR body_extracted_at < start_at);
DELETE FROM action_items WHERE event_id IN (SELECT id FROM calendar_events
  WHERE recurrence_master_id IS NOT NULL OR body_extracted_at < start_at);
```

## 8. Backup and restore

`scripts/backup_db.py` takes an MVCC-consistent snapshot of a live `brain.db`,
verifies it, and optionally writes a zstd-compressed, AES-256-encrypted offsite
copy. The restore path, its prerequisites, and the checks that tell a good
archive from a corrupt one are in [`RESTORE.md`](RESTORE.md).

**A file-level backup of the producer takes the snapshot, not the live file.**
restic, or anything like it, reads `brain.db` while the timers write it, and a
WAL-mode database copied that way can be torn. Keep the data home in its paths,
so the daily `backups/brain-YYYYMMDD.db` is what it stores, and exclude the live
file, its sidecars and the encrypted archives. For restic, these lines in the
excludes file (with your data home's absolute path), and no line excluding
`backups/` as a whole:

```text
/path/to/data-home/brain.db
/path/to/data-home/brain.db-wal
/path/to/data-home/brain.db-shm
/path/to/data-home/backups/offsite
```

Restoring from it is in [`RESTORE.md`](RESTORE.md), "restic: the copy off both
hosts".

**Drill the restore weekly.** `python scripts/backup_db.py --drill` decrypts the
newest archive into a temporary directory, runs `PRAGMA quick_check`, compares row
counts with a database you name, and records a pass in
`~/.second-brain/restore-drill.stamp`, which the health check's Backups row ages
(section 7). [`RESTORE.md`](RESTORE.md), "Exercise it", has the command and a
LaunchAgent for the consumer that pulls the archives.

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
`systemctl --user try-restart sb-mcp.service`.

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

It reads the token from `BRAIN_MCP_TOKEN_FILE`, else `~/.config/second-brain/mcp-token`,
and never prints it. It pings `sb-mcp-stats` (`BRAIN_MCP_PROBE_SLUG`), creating the
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
| `python -m src.cli hash-attachments` | Records `attachments.sha256` for files still on disk. Run it before deletion starts: afterwards the hash is the only evidence of what a file held |
| `python -m src.cli sweep-files [--apply] [--only-newer-than ISO] [--policy]` | Deletes registered files whose content is stored. Report-only by default |
| `scripts/reap_orphan_attachments.py [--apply] [--adopt-only] [--policy]` | Unregistered directories: stored duplicates go after a day, unique files are adopted after 7 days. Unregistered files inside a registered directory: stored duplicates go, unique ones stay |
| `data/state/sweep-policy.json` | `{"apply": true or false, "only_newer_than": ISO or null}`. Absent means report-only. The wrappers pass `--policy`, so this file alone decides whether anything is deleted |

The wrappers under `scripts/wrappers/systemd/` call the sweep and the reaper; copy them into
`~/.local/bin` after changing them (they are an archive, see section 3). The health check's
"Files on disk" row shows what is still held and warns when the sweep stops.

**Documents without files.** A SharePoint file linked from mail and a note a Claude session
wrote (`.md`, `.txt`, `.csv`) are stored as text only: `attachments.file_path` reads
`text:<source>:<key>`, and no file is kept. `process-sharepoint` fetches into a temporary
directory, within the nightly pass's `--deadline-s`; the conversation sync runs
`ingest-session-notes` over the transcripts new or changed since they were last read (subagent
transcripts included). Copy `sb-conversation-sync.sh` and `sb-attachment-pass.sh` into
`~/.local/bin` after this change. Zip
archives are unpacked in a temporary directory, the same file is extracted once, and long texts
are stored in full and summarised in parts. The one-time backfills are
`process-sharepoint --ingest-fetched` (the files earlier fetches left in `data/sharepoint`,
which `sweep-files` then also covers), `ingest-session-notes --all`, and
`reextract --capped --long --zip --unread`, each with a count first (`reextract --dry-run`).

**Files are read in full.** Every sheet and row of a spreadsheet, and every page of a scan or a
multi-page TIFF, OCR'd inside a two-minute budget per file; a scan it cuts short says "pages left
unread", and `reextract` with no budget finishes it. Text stops at the 2,000,000-character
ceiling, kept after a review measured 50,000,000 (3.3 GB of memory for every write to such a row,
search snippets that never finished). A file cut there, or stopped by an archive's safety limits,
says "file kept", and the sweep keeps it for good. Phase 2 summarises a text of more than 50
parts from 50 parts spread evenly across it; the whole text stays stored and searchable.

Rows the old readers read in part (spreadsheets to 20 sheets of 51 rows, scans to 30 pages,
multi-page TIFFs to the first page, archives with all of these, texts at the old 100,000 storage
cap or at the 2,000,000 ceiling) count as not stored until `reextract --partial` reads them again:
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
it and the sweep keeps its file (README, "Greek read as Latin letters"). For the texts read before
this change, on the producer, in this order, each with a count first (`--dry-run`):

1. `python -m src.cli ocr-regarble` counts them and lists the attachment ids whose files are on
   disk or swept; `ocr-regarble --apply` marks them and clears their summaries, decisions,
   actions, facts and vectors.
2. `python -m src.cli refetch-garbled --limit 200`, repeated until both its `to try` lines read
   0, fetches the swept originals back from the mailbox in two halves: at most 200 messages a
   run in all, two `outlook-cli` calls at a time, oldest mail first, since the mailbox keeps
   about a year and older mail is gone for good.
   - By id: each message is asked for under its ids. A message the mailbox no longer holds is
     recorded as gone and not asked for again; a failed download is asked for again on the next
     run, three attempts in all.
   - By folder, time and subject, within what the id half leaves of the limit: mail the retired
     Apple Mail exporter stored carries no id the mailbox knows, unless an Archive copy of it was
     recorded. Its folder (then Archive, Sent Items and Inbox) is listed over its received time,
     two minutes either side, read as Athens time; the subject must match, and a file goes back
     only when its sha256 is the row's. Several matching messages are counted as ambiguous and
     skipped for good. With no match, mail over a year old is recorded as gone, and younger mail
     as not found, looked up again on the next run, three attempts in all. `--no-lookup` runs the
     id half alone.

   Count first with `--dry-run`, which shows both halves. Exit 4 means `outlook-cli` needs a
   login: log in and run it again. The sweep keeps a fetched original until it has been read
   again.
3. `python -m src.cli reextract --garbled` reads the marked scans and images on disk again, Greek
   first, and summarises the ones that now read.
4. The nightly image pass, or `transcribe-images --limit 0` at once, transcribes the marked
   content images, and the transcription replaces the garbled text.

`ocr-regarble --apply` changes only what it has not changed before, so it can be run again: do so
after a large `reextract` pass, since a re-read that newly marks a row summarised earlier leaves
that summary for it to clear (Phase 2 never makes a new one).

**SharePoint links by kind.** A file behind a sharing or viewer link is downloaded by
`sharepoint-cli get`, which resolves the link through its viewer page; an intranet page is read
by `sharepoint-cli page` as text; a link with nothing to read is recorded `not-content` and
never fetched. A file link whose page names no file is recorded `not-a-file`: a failure the
health row shows and the retry pass offers again, since a viewer format change looks the same. Both commands need a `sharepoint-access` build that has them (`git pull && npm ci
&& npm run build` in `~/SourceCode/sharepoint-access`, or wait for repo-autoupdate). The one-time
backfill for links recorded `ok` with no text stored (mostly viewer pages saved as the file) is
`process-sharepoint --refetch-content --max-fetches 0 --deadline-s 1800`, with a count first
(`--dry-run`). It is resumable: the `sync_metadata` row `sharepoint_refetch_since` marks when it
began, a link attempted since is done, and a finished pass finds nothing. Delete that row to read
them all again.

# Deployment

The engine runs from this repo plus your own environment — no external private
repo is required. It works on macOS (launchd) and Linux (systemd/cron).

## 1. Prerequisites

- Python 3.12+
- Optional, for Microsoft 365 ingestion: [`outlook-cli`](https://github.com/weirdapps/outlook-access)
  and [`teams-cli`](https://github.com/weirdapps/teams-access) on `PATH`.
  Everything else (local docs, URLs, YouTube, conversations, your own exporter)
  works without them.

## 2. Install

```bash
git clone https://github.com/weirdapps/papadopoulos-second-brain.git
cd papadopoulos-second-brain
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 3. Configure

```bash
cp .env.example .env      # then edit .env
```

Required: `BRAIN_USER_NAME`, `BRAIN_USER_ROLE`, `BRAIN_USER_EMAIL_PATTERN`, and one
extraction path (Vertex ADC, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`). See
`.env.example` for the full list.

**Data home.** By default the DB and all ingested data live in `<repo>/data`. For
a scheduled deployment, point them at a stable, checkout-independent location so
they survive repo moves/upgrades:

```bash
export BRAIN_DATA_DIR="$HOME/.second-brain/data"
```

Everything (DB, attachments, staging, embeddings, sharepoint) derives from
`BRAIN_DATA_DIR`.

## 4. Verify

```bash
python -m src.cli stats          # initialises schema on an empty DB
python -m src.cli sync           # one ingest→extract→load cycle
```

## 5. Schedule

The pipeline is just CLI commands — schedule them however you like. Source your
`.env` first so the job inherits `BRAIN_DATA_DIR` and credentials.

**cron** (hourly incremental sync, daily embed):

```cron
7 * * * *  cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python -m src.cli sync  >> ~/.second-brain/logs/sync.log 2>&1
23 3 * * * cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python -m src.cli embed >> ~/.second-brain/logs/embed.log 2>&1
```

**Linux — systemd (user) timer.** A wrapper that sources `.env` and runs the CLI:

```ini
# ~/.config/systemd/user/sb-sync.service
[Service]
Type=oneshot
WorkingDirectory=%h/papadopoulos-second-brain
EnvironmentFile=%h/papadopoulos-second-brain/.env
ExecStart=%h/papadopoulos-second-brain/.venv/bin/python -m src.cli sync
```

```ini
# ~/.config/systemd/user/sb-sync.timer
[Timer]
OnCalendar=hourly
[Install]
WantedBy=timers.target
```

Enable: `systemctl --user enable --now sb-sync.timer`.

**macOS — launchd.** A `LaunchAgent` in `~/Library/LaunchAgents/` running a
wrapper that `cd`s into `$BRAIN_REPO`, sources `.env`, and runs
`python -m src.cli sync` on `StartInterval` (or `StartCalendarInterval`). Set
`BRAIN_LABEL_PREFIX` to namespace the labels.

## 6. Health check (optional)

`scripts/health_check.py` reports source freshness + job status and can email a
summary (needs `outlook-cli` and `HEALTH_EMAIL_TO`):

```bash
HEALTH_EMAIL_TO=you@example.com python scripts/health_check.py --email
```

## 7. Serve to Claude Code

Register the MCP server once (see `README.md` → "Register with Claude Code"); it
reads the same `BRAIN_DATA_DIR`, so queries hit the DB your scheduled jobs write.

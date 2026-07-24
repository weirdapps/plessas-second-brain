# second-brain

Personal knowledge repository and MCP data layer. It ingests emails, attachments, calendar events, Teams messages, SharePoint documents, and Claude Code conversation history into a SQLite database (`brain.db`), then exposes that store to Claude Code plugins via a Model Context Protocol (MCP) server.

The point: give the agent recall over years of institutional context (people, decisions, action items, topics, key facts) without re-reading every message on every turn.

[![CI](https://github.com/weirdapps/plessas-second-brain/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/weirdapps/plessas-second-brain/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Python 3.12+. MIT-licensed. Maintained by [@weirdapps](https://github.com/weirdapps).

## What it does

Four stages, run on a schedule:

1. **Ingest.** Pull new mail, attachments, calendar events, Teams messages, SharePoint links, standalone documents, URLs, YouTube transcripts, and Claude Code conversations into `data/staging/`.
2. **Extract.** Send raw content through an LLM (Claude via Vertex AI by default; Gemini optional) to produce structured JSON: summary, sentiment, urgency, topics, decisions, action items, people, key facts.
3. **Load.** Write rows into SQLite with FTS5 full-text indexes, embedding vectors, and thread reconstruction. Schema migrations run automatically.
4. **Serve.** Expose the store as MCP tools that Claude Code plugins (`mail`, `meetings`, `chat`, `decks`, and more) call directly.

## Architecture

```mermaid
graph TD
    O[Outlook M365<br/>outlook-cli, hourly] --> S[data/staging]
    C[Calendar events] --> S
    T[MS Teams<br/>teams-cli] --> S
    A[Attachments<br/>PDF, DOCX, XLSX, PPTX, images, RPMSG] --> S
    P[SharePoint links] --> S
    L[Local documents<br/>reverse-ingest] --> S
    W[URLs and YouTube] --> S
    K[Claude Code<br/>~/.claude/projects] --> S
    S --> E[LLM extract<br/>Claude via Vertex AI or Gemini]
    E --> D[(brain.db<br/>SQLite, FTS5, embeddings)]
    D --> M[MCP server<br/>python -m src.mcp_server]
    M --> CC[Claude Code plugins<br/>mail, meetings, chat, decks]
```

## Ingestion sources

| Source | Module | Notes |
|---|---|---|
| Microsoft 365 mail | `src/export/outlook_export.py` + `outlook_cli.py` | Primary. Hourly, `--since` cursor. |
| Apple Mail archive | `src/export/apple_mail.py` | Frozen. Kept for historical rollback. |
| Attachments | `src/export/outlook_attachments.py`, `src/extract/attachment_extractors.py` | PDF (PyMuPDF), DOCX (`python-docx`), PPTX (`python-pptx`), XLSX (`openpyxl`), XLSB (`pyxlsb`), XLS (`xlrd`), images (Tesseract OCR), EML, RPMSG (`compoundfiles`). |
| Inline email images | `src/extract/image_classifier.py`, `image_pipeline.py`, `image_vision.py` | Dimensions plus bytes plus sender-scoped SHA256 dedup cascade; vision LLM stage for content images, cached by SHA256. |
| Calendar events | `src/export/calendar_export.py`, `src/extract/calendar_extractor.py` | Outlook events with attendees, body summary, decisions. |
| MS Teams | `src/export/teams_cli.py`, `teams_export.py`, `src/extract/teams_pipeline.py` | Chats, threads, messages, MRI resolution. |
| SharePoint links | `src/extract/sharepoint_url_scanner.py`, `src/export/sharepoint_fetcher.py` | Managed host defaults to `contoso.sharepoint.com` (override via `SHAREPOINT_HOST`). |
| Standalone documents | `src/cli.py ingest`, `reverse-ingest`, `src/ingest/reverse_scan.py` | Latest-version-per-logical-name dedup. |
| Web and YouTube | `src/extract/web_ingest.py` | URL fetch plus transcript pull via `youtube-transcript-api`. |
| Claude Code sessions | `src/export/conversation_export.py` | Reads `~/.claude/projects`. |

### Bring your own source

The extract → load → serve chain is **source-agnostic**: it only reads staging batches from `data/staging/batch-*.json`. The Microsoft 365 adapters above are just one way to fill that folder. To ingest from any other system (Gmail, IMAP, an `.mbox`, a custom API), write a small exporter that emits the same JSON shape — no other code changes needed.

A staging batch is `{ "batch_number", "exported_at", "source", "folder", "emails": [ … ] }`, where each email record is:

```json
{
  "message_id": "any-stable-unique-id",
  "date_received": "2026-01-15T09:00:00Z",
  "subject": "…",
  "sender":        { "name": "…", "address": "…" },
  "to_recipients": [{ "name": "…", "address": "…" }],
  "cc_recipients": [],
  "mailbox_name": "Inbox",
  "content": "plain-text or HTML body",
  "conversation_id": "optional — for threading",
  "internet_message_id": "optional — for cross-source dedup"
}
```

See [`examples/example_exporter.py`](examples/example_exporter.py) for a ~40-line reference exporter and [`examples/sample-batch.json`](examples/sample-batch.json) for a complete synthetic batch. Drop a batch into `data/staging/`, then run `python -m src.cli sync` (or `load`) to extract and index it.

## MCP tools

The MCP server exposes 23 tools (all defined in `src/mcp_server.py`). Register the server once in `~/.claude/settings.json`, then every session picks them up.

### Unified recall

- `recall(query, limit_per_kind, days)`. Fan-out across every text-bearing index (emails, attachments, standalone documents, conversations, decisions, actions, inline images) plus auto-pulled person and topic context. This is the default "tell me everything you know about X" entry point.

### Emails

- `search_emails(query, search_type, limit)`. Keyword (FTS5) or semantic (embedding).
- `query_emails(person, topic, keyword, start_date, end_date, limit)`. Combined filters.
- `outlook_live_search(folder, since_minutes, subject_contains)`. Bypasses the DB and queries the live Outlook mailbox for messages younger than one hour that have not been ingested yet.

### People and topics

- `person_context(name_or_email, days)`. History, sentiment, decisions, open actions, communication pattern.
- `topic_context(topic, days)`. Key people, decisions, actions, facts.
- `sender_brief(name_or_email, days)`. Compact briefing suitable for inline display.
- `meeting_prep(people, topic, days)`. Per-attendee dossiers, optionally scoped to a topic.

### Decisions and actions

- `query_decisions(topic, person, days, limit)`.
- `query_actions(owner, status, limit)`.
- `stale_threads(days)`. Threads awaiting reply plus overdue action items.

### Attachments and images

- `search_attachments(query, limit)`. FTS over extracted text and LLM summaries.
- `attachment_image_search(query, limit)`. LIKE match on vision descriptions of classified content images.

### Calendar

- `query_calendar_events(person, since, until, keyword, limit)`.

### Teams

- `search_teams(query, kind, limit)`. Thread summaries, message text, or both.
- `teams_thread_context(thread_id)`. Full thread with decisions, actions, facts.
- `teams_chat_summary(chat_id, days)`. Recent activity per chat or channel.

### SharePoint

- `sharepoint_index(operation, url)`. `list_stale`, `list_unfetched`, or `refetch` a specific URL.

### Claude Code conversation memory

- `search_conversations(query, search_type, workspace, limit)`.
- `conversation_context(session_id)`.
- `recall_preference(topic, limit)`. Surface prior user preferences and corrections extracted from past sessions.
- `recent_conversations(workspace, days, limit)`.

### Stats

- `stats()`. Counts across emails, conversations, topics, people, decisions, actions, attachments, calendar events.

## Installation

Requires Python 3.12+. For Microsoft 365 ingestion (mail, Teams, calendar, SharePoint) you also need [`outlook-cli`](https://github.com/weirdapps/outlook-access) and [`teams-cli`](https://github.com/weirdapps/teams-access) on `PATH` — both are open source. Everything else (local documents, URLs, YouTube, Claude Code conversations, or your own exporter) works without them.

```bash
git clone git@github.com:weirdapps/plessas-second-brain.git
cd plessas-second-brain

python3.12 -m venv .venv
source .venv/bin/activate

# uv is the canonical package manager (uv.lock is committed):
uv pip install -e ".[dev]"

# pip also works:
pip install -e ".[dev]"
```

## Configuration

Read from environment variables (shell profile or `.env`). Only identity plus one extraction path (Vertex or Gemini) is strictly required.

### Identity

Used in extraction prompts and stale-thread detection.

- `BRAIN_USER_NAME`
- `BRAIN_USER_ROLE`
- `BRAIN_USER_EMAIL_PATTERN` (case-insensitive substring, matched against `sender_address` to detect your sent mail)

### Extraction engine

- `BRAIN_EXTRACT_ENGINE`: `claude` (default) or `gemini`.
- `CLAUDE_EXTRACT_MODEL` or `VERTEX_MODEL_EXTRACT`: Claude model override (default `claude-sonnet-4-6`).
- `BRAIN_GEMINI_MODEL`: Gemini model override (default `gemini-2.5-flash`).
- `BRAIN_TEAMS_MODEL`: override for Teams thread extraction.

### Vertex AI

Preferred credential path. Uses Application Default Credentials, no API key required.

- `VERTEX_SDK_PROJECT` or `ANTHROPIC_VERTEX_PROJECT_ID`: GCP project id.
- `VERTEX_SDK_REGION` or `CLOUD_ML_REGION`: region. Model-region pairing matters. Claude 4.7 and newer requires `eu`; 4.6 and older requires `europe-west1`. A mismatch returns HTTP 429.
- `VERTEX_MODEL_FALLBACK_SDK` (default `claude-opus-4-6`) and `VERTEX_REGION_FALLBACK` (default `europe-west1`): auto-downgrade target on policy refusals.
- `VERTEX_REGION_EMBED`: embedding region (default `europe-west1`).

### Alternative credentials

- `ANTHROPIC_API_KEY`: direct Anthropic API. Used only if no Vertex credentials are found.
- `GEMINI_API_KEY`: required when `BRAIN_EXTRACT_ENGINE=gemini`.

### Paths and hosts

- `SHAREPOINT_HOST`: SharePoint tenant you hold a session for (default `contoso.sharepoint.com`). Auth failures on any other host are recorded as `unsupported-host` and skipped.
- `SECOND_BRAIN_VENV_PYTHON`: explicit venv override for `run_mcp.sh`.
- Database defaults to `data/brain.db` inside the repo root. The global `--db` flag (placed before the subcommand, e.g. `python -m src.cli --db /path/brain.db stats`) overrides it.

No credentials are printed by the code. All secrets are read from environment or (for Google) from ADC.

## Usage

### Run the MCP server

```bash
./run_mcp.sh
# or, directly:
python -m src.mcp_server
```

`run_mcp.sh` auto-detects the venv in this order: `$SECOND_BRAIN_VENV_PYTHON`, `./.venv/bin/python`, `./venv/bin/python`, `~/.venvs/second-brain/bin/python`, then `python3`. The script is portable across hosts (in-repo venv on macOS, out-of-repo `~/.venvs/` on the VPS).

### Register with Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/absolute/path/to/second-brain/run_mcp.sh"
    }
  }
}
```

In any Claude Code session, ask "what do we know about X" and the agent calls `recall`. The `mail`, `meetings`, `chat`, and `decks` marketplace plugins consume these tools automatically.

### CLI

`./brain` (wrapper) or `python -m src.cli`. Highlights:

```bash
# End-to-end incremental sync (export, extract, load, dedup, embed, Teams, images, conversations)
python -m src.cli sync --engine claude --workers 4

# Ingestion by source
python -m src.cli calendar-sync --since 2026-01-01
python -m src.cli teams-sync --workers 4
python -m src.cli process-attachments --phase 2 --workers 2
python -m src.cli process-images --limit 500
python -m src.cli process-sharepoint --since 2026-06-01
python -m src.cli reverse-ingest --root ~/Documents --workers 4
python -m src.cli ingest ~/Downloads/report.pdf --source "Q2 report"
python -m src.cli ingest --url https://example.com/article

# Queries (CLI mirrors of the MCP tools)
python -m src.cli query keyword "budget approval"
python -m src.cli query semantic "concerns about digital transformation"
python -m src.cli query person "Duarte"
python -m src.cli query topic "cards migration"
python -m src.cli query decisions --topic cards
python -m src.cli query actions --owner Chen --status open
python -m src.cli query combined --person "Chen" --topic "digital"
python -m src.cli prep "Chen,Okafor" --topic digital
python -m src.cli stale --days 5
python -m src.cli stats

# Housekeeping
python -m src.cli migrate
python -m src.cli embed --force
python -m src.cli prune-staged
```

Full subcommand list: `python -m src.cli --help`.

## Code layout

```text
src/
  cli.py                       Command-line entry (`brain` wrapper points here)
  config.py                    Paths, env-driven settings, schema version
  mcp_server.py                MCP server (FastMCP), 23 tools
  bridge.py                    Legacy JSON-over-CLI bridge (superseded by MCP)
  export/
    outlook_export.py          Hourly Outlook ingestion via outlook-cli
    outlook_cli.py             outlook-cli subprocess wrapper
    outlook_attachments.py     Attachment fetch for Outlook messages
    apple_mail.py              Historical Apple Mail export (frozen)
    attachments.py             Apple Mail attachment export
    attachment_state.py        Per-attachment fetch cursor
    calendar_export.py         Outlook calendar events
    conversation_export.py     Claude Code session transcripts
    teams_cli.py               teams-cli subprocess wrapper
    teams_export.py            Teams chats, threads, messages
    inbox_reconcile.py         Cursor recovery
    sharepoint_fetcher.py      SharePoint link fetch and host classification
    state.py                   Export state
  extract/
    prompt.py                  Email extraction prompt
    attachment_prompt.py       Attachment summarization prompt
    teams_prompt.py            Teams thread extraction prompt
    local.py                   Concurrent extraction dispatcher
    parser.py                  Tolerant LLM JSON parser
    claude_extract.py          Claude via Vertex AI or direct API
    vertex_auth.py             Vertex AI credential resolution
    vertex_fallback.py         Model and region fallback on policy refusals
    attachment_pipeline.py     Two-phase pipeline (text extraction, then LLM)
    attachment_extractors.py   PDF, DOCX, PPTX, XLSX, XLSB, XLS, image, EML, RPMSG
    image_classifier.py        Dimensions plus bytes plus SHA256 dedup cascade
    image_vision.py            Vision LLM stage
    image_pipeline.py          Orchestration and backfill
    web_ingest.py              URL and YouTube ingestion
    calendar_extractor.py      Calendar body summarization
    teams_mri.py               Teams MRI to display-name resolution
    teams_pipeline.py          Thread extraction dispatcher
    teams_threads.py           Message to thread bounding
    sharepoint_url_scanner.py  Detect SharePoint URLs in email bodies
  store/
    schema.py                  Tables, FTS5 indexes, migrations
    loader.py                  Extracted JSON to SQLite
    query.py                   Person, topic, keyword, date, decisions, actions, FTS, combined
    context.py                 Rich person and topic context aggregations
    embeddings.py              Embedding index build and query
    recall.py                  Unified `recall` fan-out
    normalizer.py              Greek-aware topic and people normalization
    dedup_people.py            Six-phase people deduplication
    conversation_query.py      Claude Code conversation search
    teams_query.py             Teams thread, chat, and search
    calendar_loader.py         Calendar event and attendee loader
  ingest/
    reverse_scan.py            Filesystem scan with latest-version-per-name dedup

scripts/
  backup_db.py                 MVCC-safe encrypted DB snapshot + retention
  recover_missing_extractions.py  Backfill emails that staged but never extracted
  backfill-all.sh              One-shot backfill across all sources
  conversation-capture.sh      Helper to snapshot Claude Code sessions
  pii-gauntlet.sh              Guards tracked files against personal-data leaks

examples/
  example_exporter.py          Reference "bring your own source" exporter
  sample-batch.json            A complete synthetic staging batch

skill/
  recall.md                    `/recall` Claude Code slash command
```

## Database schema

`data/brain.db` (SQLite). Migrations run automatically via `src/store/schema.py`; the current version is `CURRENT_SCHEMA_VERSION = 14` in `src/config.py` and is tracked in the `schema_version` table.

- **Core content**: `emails`, `topics`, `email_topics`, `decisions`, `action_items`, `people`, `email_people`, `key_facts`
- **Attachments and images**: `attachments`, `attachment_content`, `inline_images`, `inline_image_occurrences`, `sender_signature_index`
- **Calendar**: `calendar_events`, `event_attendees`
- **Teams**: `teams_chats`, `teams_threads`, `teams_messages`, `teams_mri_resolution`
- **Conversations**: `conversations`, `conversation_turns`, `conversation_topics`
- **External refs**: `sharepoint_links`
- **FTS5**: `emails_fts`, `key_facts_fts`, `attachment_content_fts`, `conversation_turns_fts`, `conversations_fts`, `teams_messages_fts`, `teams_threads_fts`, `calendar_events_fts`
- **Metadata**: `sync_metadata` (per-source cursors), `schema_version`

Thread identity: primary anchor is Outlook `ConversationId`; fallback is subject normalization with Greek `Re:` and `Fwd:` awareness.

## Development

### Tests

pytest suite under `tests/`, grouped into top-level tests plus focused subpackages (`tests/export`, `tests/extract`, `tests/mcp`, `tests/store`, `tests/teams`).

```bash
pytest                              # full suite
pytest tests/test_mcp_server.py     # single file
pytest -k recall                    # filter by expression
pytest --cov=src --cov-report=term  # with coverage
```

### Lint and format

```bash
ruff check .
ruff format --check .
```

Pre-commit hooks are wired via `.pre-commit-config.yaml` (`ruff`, `mypy`, `gitleaks`, `markdownlint`, plus a `pii-gauntlet` gate that blocks personal data from entering tracked files).

### CI

- `.github/workflows/ci.yml`: `ruff check`, `ruff format --check`, then `pytest` on every push and PR to `master`.
- `.github/workflows/sonarcloud.yml`: SonarCloud coverage upload (skipped on private repos by design; runs only when `SONAR_TOKEN` is present and the repo is public).
- `.github/workflows/dependabot-auto-merge.yml`: auto-merge for green Dependabot PRs.

Dependabot is configured for the `uv` ecosystem (see `.github/dependabot.yml`), so PRs update `uv.lock`.

### Scheduling

The pipeline is just CLI commands, so schedule them however you like. Examples:

- **cron** (hourly incremental sync): `7 * * * * cd /path/to/repo && .venv/bin/python -m src.cli sync >> ~/second-brain.log 2>&1`
- **macOS launchd** / **systemd timers**: wrap `python -m src.cli sync` (and `embed`) in a service unit pointing at your checkout and venv.

Typical cadence: `sync` hourly, `embed` daily.

## Security

Report vulnerabilities via GitHub's private vulnerability reporting — see `SECURITY.md`.

## License

MIT. See `LICENSE`.

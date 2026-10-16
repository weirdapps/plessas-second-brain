# second-brain

Multi-modal personal knowledge base: ingests emails, attachments, calendar events, Teams messages, SharePoint links, standalone documents, URLs, YouTube transcripts, news digests, and Claude Code conversation history through an LLM extraction pipeline into a queryable SQLite database, exposed via an MCP server. See `README.md` for the full architecture, MCP tool list, and configuration.

## Tech stack

- **Python 3.12+**, installed with `uv sync --frozen` (`uv.lock` is committed; `--extra dev` adds the test tools). `pyproject.toml` declares no build system, so nothing is installed as a package: run from the checkout (`./brain`, `./run_mcp.sh`, or `python -m src.cli` from its root). `pip install -e .` ignores the lock and puts `src/` itself on the path, not the checkout.
- Runtime deps (see `pyproject.toml`): `anthropic[vertex]`, `google-genai` (embeddings and the Gemini engine), `mcp[cli]`, `numpy`, `pymupdf`, `python-docx`, `python-pptx`, `openpyxl`, `pyxlsb`, `xlrd`, `pytesseract`, `pillow`, `pillow-heif`, `youtube-transcript-api`, `snowballstemmer` (Greek query stemming), and `anyio` (a security floor on a transitive dependency). None is source-only, so CI installs with `--no-build` and runs no `setup.py`.
- System packages `pip` cannot install: **`tesseract` plus the `eng` and `ell` traineddata** (`attachment_extractors.py` reads `lang="ell+eng"`, and `eng+ell` again for a page that comes out as Latin script; `pytesseract` is only the wrapper), **`antiword` or `catdoc`** on Linux for legacy `.doc` attachments (macOS uses `textutil`; with none of the three every `.doc` is skipped), and **`zstd` + `openssl`** for `scripts/backup_db.py` offsite snapshots. Verify with `tesseract --list-langs | grep -x ell`.
- The dev extra holds the test tools. **`ruff` is not in it**: CI pins `ruff==0.16.8` and pre-commit pins the same `v0.16.8` (keep them in lockstep), so run `uvx ruff@0.16.8 check .` locally. Pre-commit: `ruff` (check + format), `mypy`, `gitleaks`, `yamllint` (workflows only), `markdownlint`, and a `pii-gauntlet` gate.
- DB: SQLite at `data/brain.db` (override with `--db`, or relocate the whole data home with `BRAIN_DATA_DIR`, which must be an absolute path). `data/` is gitignored and never committed.

## Running

- MCP server: `./run_mcp.sh` (auto-detects the venv: `$SECOND_BRAIN_VENV_PYTHON`, `./.venv`, `./venv`, `~/.venvs/second-brain`, then `python3`). Over HTTP: `python -m src.mcp_server --http 127.0.0.1:8765` (loopback only, bearer token from the file `BRAIN_MCP_TOKEN_FILE` names; see `README.md`).
- CLI: `python -m src.cli --help` (or the `./brain` wrapper, which runs from any directory). Reads go through `brain tool <name> '<json>'` (the MCP tools, JSON out; `brain tool --list`); one-off maintenance is under `brain maint`. The jobs with several stages (sync, teams-sync, whatsapp-sync, calendar-sync, process-sharepoint) live in `src/pipeline/`; `src/cli.py` parses their arguments.
- Incremental sync over staged mail (extract → load → register attachments → people dedup → attachment text and summaries → Claude Code conversations → inline images → one embedding pass): `python -m src.cli sync`. Mail is staged by `python -m src.export.outlook_export` (outlook-cli); `sync` never exports it. Teams, WhatsApp, calendar, news and SharePoint are separate subcommands with their own schedule; `docs/DEPLOY.md` section 6 has the stages and what runs hourly and nightly.

## Hosts

- The producer, a Linux VPS in this deployment, runs every ingest job on `systemd --user` timers and owns the only writable `brain.db`. For the brain bot it also runs `sb-mcp`, the HTTP mode as a long-running `systemd --user` unit with `BRAIN_ROLE=replica` (`docs/DEPLOY.md` section 9). A `git pull` there changes the code and nothing else: `uv sync --frozen` when `uv.lock` changed, `systemctl --user try-restart sb-mcp.service` (sb-mcp keeps serving the code it started with), and an `install` into `~/.local/bin` of each wrapper that changed are separate steps (`docs/DEPLOY.md` section 12). The wrappers the units run are archived in `scripts/wrappers/systemd/`.
- A Mac is a replica: an hourly pull at :45 (`scripts/wrappers/launchd/sb-db-pull.sh`) copies `brain.db` and `embeddings.npz` down, and the local MCP server reads that copy. `run_mcp.sh` gives the server its own settings (the embedding key and backend, for instance) from the owner-only file `~/.second-brain/env` (`$BRAIN_DATA_DIR/env` when that is set), since Claude Code may start it with no shell profile. Every writer refuses to run on a replica: the `python -m src.cli` subcommands that write, the action lifecycle, the store maintenance modules, the scripts that write and the MCP `sharepoint_index` refetch (`BRAIN_ROLE=replica`, or the pull's stamp `~/.second-brain/db-pull.stamp`); `BRAIN_ROLE=producer` overrides the stamp.
- Schema migrations run on the producer, when the first job that writes opens the store after a pull, and reach replicas with the next pull.

## Tests

```bash
pytest                          # full suite
pytest tests/test_mcp_server.py # single file
pytest -k recall                # filter by expression
pytest --cov=src --cov-report=term
```

`tests/conftest.py` redirects the data root to a temp dir in `pytest_configure` (before collection, because `src/config.py` resolves paths at import time) and blocks sockets for the whole session. Do not re-do either by hand in a test, and do not assume this machine's `data/` tree exists on CI.

CI (`.github/workflows/ci.yml`), five jobs on every push and PR to `master`: `lint` (`ruff check` + `ruff format --check`), `test` (`uv sync --frozen --no-build --extra dev`, then `pytest` with coverage over `src` and `scripts`, failing below 85%), `types` (pinned `mypy` over `src/` and `scripts/`), `pii-gauntlet`, and `wrappers` (parses each `scripts/wrappers/` script with its declared interpreter). `test` and `types` run on Python 3.12 and 3.14.

## Architecture (four stages)

1. **Ingest** → raw content into `data/staging/batch-*.json`. The staging JSON shape is the source-agnostic contract (see "Bring your own source" in `README.md` and `examples/`). Microsoft 365 sources use the external, optional `outlook-cli` / `teams-cli` / `sharepoint-cli` adapters. `news-sync` reads an external news-reader SQLite DB (`BRAIN_NEWS_DB`) and stages digests plus above-threshold articles under `mailbox_name = 'News'`.
2. **Extract** → an LLM (Claude via Vertex AI by default; the direct Anthropic API only when switched on with `BRAIN_ALLOW_DIRECT_API=1`; Gemini optional) produces structured JSON: summary, sentiment, urgency, topics, decisions, action items, people, key facts.
3. **Load** → `src/store/loader.py` writes to SQLite with FTS5 indexes + embedding vectors + thread reconstruction. `create_database` stamps version 0 and then calls `run_migrations`, so a fresh store and a migrated one converge on the same tables; `CURRENT_SCHEMA_VERSION` lives in `src/config.py`. `python -m src.cli load` is the only command that creates the DB: `tool`, `sync` and `migrate` all exit 1 if it is absent.
4. **Serve** → `src/mcp_server.py` (`mcp.server.MCPServer`, mcp SDK v2) exposes the store as MCP tools; `brain tool <name> '<json>'` calls any of them from the terminal through the server's own call (`src/mcp_call.py`), JSON out.

## Key conventions

- **Extraction engine** (`src/extract/claude_extract.py`): Vertex AI + ADC whenever a project is set (`VERTEX_SDK_PROJECT` / `ANTHROPIC_VERTEX_PROJECT_ID`). The direct Anthropic API only with `ANTHROPIC_API_KEY` and `BRAIN_ALLOW_DIRECT_API=1` and no Vertex project: a key alone does nothing. Otherwise the client raises. The backend, region and model go to stderr when the client is built. Gemini via `BRAIN_EXTRACT_ENGINE=gemini` + `GEMINI_API_KEY`.
- **One way to call Claude**: every call site sends through `claude_extract.complete()`, which carries the shared client, the retry and re-auth policy, and the refusal fallback. A test fails if anything else calls `messages.create`.
- **Model and region are required**: `CLAUDE_EXTRACT_MODEL` / `VERTEX_MODEL_EXTRACT` and `VERTEX_SDK_REGION` / `CLOUD_ML_REGION` have no default; unset, the first Claude call stops with a message naming them and the work stays pending. Pairing: Claude 4.7+ → `eu`; 4.6 and older → `europe-west1`. A mismatch returns HTTP 429.
- **Call shape**: `BRAIN_EXTRACT_CALL_SHAPE` is `v2-text` by default (since 2026-10-16: instructions in a cached system prompt with schema v2 stated in it, pinned effort, no structured outputs, which the owner's Vertex project refuses), `v1` (the requests as built before 2026-10-11, the way back) or `v2` (schema v2 through structured outputs, for a project that allows them); `src/extract/call_shape.py`, `src/extract/schema_v2.py`. Any other value stops the calls. The loader reads v1 and v2 extractions alike, and fills the v35 item-state columns from v2 ones; news and automation mail give no decisions, actions or commitments.
- **Embeddings** (`src/store/embeddings.py`): `gemini-embedding-001` through the Gemini API when `GEMINI_API_KEY` is set (or `BRAIN_EMBED_BACKEND=gemini`), else through Vertex (`VERTEX_REGION_EMBED`); `BRAIN_EMBED_BACKEND=vertex` keeps Vertex on a host that holds a key. Same model, so the vectors share one index. A query that cannot be embedded is ranked around its top keyword matches and marked `semantic: keyword_seeded`.
- **Identity**: `BRAIN_USER_NAME` / `BRAIN_USER_ROLE` / `BRAIN_USER_EMAIL_PATTERN` feed extraction prompts and stale-thread detection. `BRAIN_USER_EMAIL_PATTERN` lists the owner's whole addresses, primary and aliases, separated by commas or spaces; each is matched exactly and case-blind, an entry with no domain matches that local part at any domain, and nothing else is the owner (`src/store/owner.py`). It decides sent mail, automation mail, calendar self flags and the people row that is the owner.
- **Staging batches**: `data/staging/batch-NNNNN.json`; same shape regardless of source, so extract + load are source-agnostic.
- **Thread identity**: primary = Outlook `ConversationId`; fallback = subject normalization stripping English (`Re:` / `Fwd:` / `FW:`) and Greek (`Απ:` / `Πρ:`) prefixes.
- **Privacy**: `data/` is gitignored; `scripts/pii-gauntlet.sh` blocks personal data from tracked files. Keep it green before every push.
- **Deployment**: `docs/DEPLOY.md` covers prerequisites, bootstrap, the producer/replica topology (including `loginctl enable-linger`), the schedule, the `HC_PING_URL` dead man's switch, and upgrading a producer with its one-off commands in order (section 12). `docs/RESTORE.md` covers the backup artefacts, the restore path, restic and the weekly restore drill.

## M365 access: three CLIs, not one (2026-08-08)

They share no session and no login. One surface each:

- **Mail, calendar, attachments** go through `src/export/outlook_cli.py` ->
  `outlook-cli`. Overridable with `OUTLOOK_CLI_PATH`.
- **Teams chats and channels** go through `src/export/teams_cli.py` ->
  `teams-cli`, which must be on `PATH`.
- **SharePoint reference attachments** go through `src/export/sharepoint_cli.py`
  -> `sharepoint-cli` (`~/SourceCode/sharepoint-access`), reading
  `~/.sharepoint-cli/session.json`. Overridable with `SHAREPOINT_CLI_PATH`.

Exit codes 0-6 are identical across all three, so 4 always means
"re-authenticate" and 5 always means "upstream misbehaved".

`sharepoint_fetcher.py` is the adapter and keeps the original `FetchStatus`
vocabulary, so the `sharepoint_links` table and the `sharepoint_index` MCP tool
are unaffected. `scripts/health_check.py` watches the new session path.

It previously did `session["bearer"]`, but this tenant is MCAS-gated and issues
no SharePoint bearer, so that path had been failing with `KeyError: 'bearer'`.
Auth is cookie-based now.

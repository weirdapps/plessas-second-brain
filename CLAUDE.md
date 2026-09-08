# second-brain

Multi-modal personal knowledge base: ingests emails, attachments, calendar events, Teams messages, SharePoint links, standalone documents, URLs, YouTube transcripts, news digests, and Claude Code conversation history through an LLM extraction pipeline into a queryable SQLite database, exposed via an MCP server. See `README.md` for the full architecture, MCP tool list, and configuration.

## Tech stack

- **Python 3.12+**. `uv` is the canonical package manager (`uv.lock` is committed); `pip install -e ".[dev]"` also works.
- Runtime deps (see `pyproject.toml`): `anthropic[vertex]`, `google-genai` (embeddings; `google-cloud-aiplatform` was dropped in 59b5fde, it imports nowhere here and only supplied `google-genai` by transitive accident), `mcp[cli]`, `numpy`, `pymupdf`, `python-docx`, `python-pptx`, `openpyxl`, `pyxlsb`, `xlrd`, `pytesseract`, `pillow`, `pillow-heif`, `compoundfiles`, `youtube-transcript-api`.
- System packages `pip` cannot install: **`tesseract` plus the `eng` and `ell` traineddata** (`attachment_extractors.py` calls `lang="eng+ell"`, and `pytesseract` is only the wrapper), and **`zstd` + `openssl`** for `scripts/backup_db.py` offsite snapshots. Verify with `tesseract --list-langs | grep -x ell`.
- Dev extra is `pytest` + `pytest-cov` only. **`ruff` is not in it**: CI pins `ruff==0.15.13` and pre-commit pins `v0.15.12`, so run `uvx ruff@0.15.13 check .` locally. Pre-commit: `ruff` (check + format), `mypy`, `gitleaks`, `yamllint` (workflows only), `markdownlint`, and a `pii-gauntlet` gate.
- DB: SQLite at `data/brain.db` (override with `--db`, or relocate the whole data home with `BRAIN_DATA_DIR`, which must be an absolute path). `data/` is gitignored and never committed.

## Running

- MCP server: `./run_mcp.sh` (auto-detects the venv: `$SECOND_BRAIN_VENV_PYTHON`, `./.venv`, `./venv`, `~/.venvs/second-brain`, then `python3`).
- CLI: `python -m src.cli --help` (or the `./brain` wrapper).
- Incremental sync (export → extract → load → attachments → dedup → embed → conversations → images): `python -m src.cli sync`. Teams, calendar, news, SharePoint and the filesystem scan are separate subcommands and need their own schedule.

## Tests

```bash
pytest                          # full suite
pytest tests/test_mcp_server.py # single file
pytest -k recall                # filter by expression
pytest --cov=src --cov-report=term
```

`tests/conftest.py` redirects the data root to a temp dir in `pytest_configure` (before collection, because `src/config.py` resolves paths at import time) and blocks sockets for the whole session. Do not re-do either by hand in a test, and do not assume this machine's `data/` tree exists on CI.

CI (`.github/workflows/ci.yml`), four jobs on every push and PR to `master`: `lint` (`ruff check` + `ruff format --check`), `test` (`uv sync --frozen --extra dev`, then `pytest` with coverage over `src` and `scripts`), `pii-gauntlet`, and `wrappers` (parses each `scripts/wrappers/` script with its declared interpreter).

## Architecture (four stages)

1. **Ingest** → raw content into `data/staging/batch-*.json`. The staging JSON shape is the source-agnostic contract (see "Bring your own source" in `README.md` and `examples/`). Microsoft 365 sources use the external, optional `outlook-cli` / `teams-cli` / `sharepoint-cli` adapters. `news-sync` reads an external news-reader SQLite DB (`BRAIN_NEWS_DB`) and stages digests plus above-threshold articles under `mailbox_name = 'News'`.
2. **Extract** → an LLM (Claude via Vertex AI by default, or `ANTHROPIC_API_KEY`; Gemini optional) produces structured JSON: summary, sentiment, urgency, topics, decisions, action items, people, key facts.
3. **Load** → `src/store/loader.py` writes to SQLite with FTS5 indexes + embedding vectors + thread reconstruction. `create_database` stamps version 0 and then calls `run_migrations`, so a fresh store and a migrated one converge on the same tables; `CURRENT_SCHEMA_VERSION` lives in `src/config.py`. `python -m src.cli load` is the only command that creates the DB: `stats`, `sync` and `migrate` all exit 1 if it is absent.
4. **Serve** → `src/mcp_server.py` (`mcp.server.MCPServer`, mcp SDK v2) exposes the store as MCP tools; `src/cli.py` mirrors them for the terminal.

## Key conventions

- **Extraction engine** (`src/extract/claude_extract.py`): if `ANTHROPIC_API_KEY` is set it wins (direct API); otherwise Vertex AI + ADC (`VERTEX_SDK_PROJECT` / `ANTHROPIC_VERTEX_PROJECT_ID` + `VERTEX_SDK_REGION` / `CLOUD_ML_REGION`); otherwise the client raises. Gemini via `BRAIN_EXTRACT_ENGINE=gemini` + `GEMINI_API_KEY`.
- **Vertex model/region pairing**: Claude 4.7+ → `eu`; 4.6 and older → `europe-west1`. A mismatch returns HTTP 429. Override the model with `CLAUDE_EXTRACT_MODEL` / `VERTEX_MODEL_EXTRACT`.
- **Identity**: `BRAIN_USER_NAME` / `BRAIN_USER_ROLE` / `BRAIN_USER_EMAIL_PATTERN` feed extraction prompts and stale-thread detection.
- **Staging batches**: `data/staging/batch-NNNNN.json`; same shape regardless of source, so extract + load are source-agnostic.
- **Thread identity**: primary = Outlook `ConversationId`; fallback = subject normalization stripping English (`Re:` / `Fwd:` / `FW:`) and Greek (`Απ:` / `Πρ:`) prefixes.
- **Privacy**: `data/` is gitignored; `scripts/pii-gauntlet.sh` blocks personal data from tracked files. Keep it green before every push.
- **Deployment**: `docs/DEPLOY.md` covers prerequisites, bootstrap, the producer/replica topology (including `loginctl enable-linger`), and the `HC_PING_URL` dead man's switch. `docs/RESTORE.md` covers the backup artefacts and the restore path.

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

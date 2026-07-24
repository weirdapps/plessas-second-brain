# second-brain

Multi-modal personal knowledge base: ingests emails, attachments, calendar events, Teams messages, SharePoint links, standalone documents, URLs, YouTube transcripts, and Claude Code conversation history through an LLM extraction pipeline into a queryable SQLite database, exposed via an MCP server. See `README.md` for the full architecture, MCP tool list, and configuration.

## Tech stack

- **Python 3.12+**. `uv` is the canonical package manager (`uv.lock` is committed); `pip install -e ".[dev]"` also works.
- Runtime deps (see `pyproject.toml`): `anthropic[vertex]`, `google-cloud-aiplatform`, `mcp[cli]`, `numpy`, `pymupdf`, `python-docx`, `python-pptx`, `openpyxl`, `pyxlsb`, `xlrd`, `pytesseract`, `pillow`, `compoundfiles`, `youtube-transcript-api`.
- Dev: `pytest`, `pytest-cov`. Linter: `ruff`. Pre-commit: `ruff` (check + format), `mypy`, `gitleaks`, `yamllint` (workflows only), `markdownlint`, and a `pii-gauntlet` gate.
- DB: SQLite at `data/brain.db` (override with `--db`). `data/` is gitignored and never committed.

## Running

- MCP server: `./run_mcp.sh` (auto-detects the venv: `$SECOND_BRAIN_VENV_PYTHON`, `./.venv`, `./venv`, `~/.venvs/second-brain`, then `python3`).
- CLI: `python -m src.cli --help` (or the `./brain` wrapper).
- Incremental sync (export → extract → load → dedup → embed): `python -m src.cli sync`.

## Tests

```bash
pytest                          # full suite
pytest tests/test_mcp_server.py # single file
pytest -k recall                # filter by expression
pytest --cov=src --cov-report=term
```

CI (`.github/workflows/ci.yml`): `ruff check` + `ruff format --check`, then `pytest`, plus a `pii-gauntlet` job, on every push and PR to `master`.

## Architecture (four stages)

1. **Ingest** → raw content into `data/staging/batch-*.json`. The staging JSON shape is the source-agnostic contract (see "Bring your own source" in `README.md` and `examples/`). Microsoft 365 sources use the external, optional `outlook-cli` / `teams-cli` adapters.
2. **Extract** → an LLM (Claude via Vertex AI by default, or `ANTHROPIC_API_KEY`; Gemini optional) produces structured JSON: summary, sentiment, urgency, topics, decisions, action items, people, key facts.
3. **Load** → `src/store/loader.py` writes to SQLite with FTS5 indexes + embedding vectors + thread reconstruction. Migrations auto-run via `src/store/schema.py` (`CURRENT_SCHEMA_VERSION` in `src/config.py`).
4. **Serve** → `src/mcp_server.py` (FastMCP) exposes the store as MCP tools; `src/cli.py` mirrors them for the terminal.

## Key conventions

- **Extraction engine** (`src/extract/claude_extract.py`): if `ANTHROPIC_API_KEY` is set it wins (direct API); otherwise Vertex AI + ADC (`VERTEX_SDK_PROJECT` / `ANTHROPIC_VERTEX_PROJECT_ID` + `VERTEX_SDK_REGION` / `CLOUD_ML_REGION`); otherwise the client raises. Gemini via `BRAIN_EXTRACT_ENGINE=gemini` + `GEMINI_API_KEY`.
- **Vertex model/region pairing**: Claude 4.7+ → `eu`; 4.6 and older → `europe-west1`. A mismatch returns HTTP 429. Override the model with `CLAUDE_EXTRACT_MODEL` / `VERTEX_MODEL_EXTRACT`.
- **Identity**: `BRAIN_USER_NAME` / `BRAIN_USER_ROLE` / `BRAIN_USER_EMAIL_PATTERN` feed extraction prompts and stale-thread detection.
- **Staging batches**: `data/staging/batch-NNNNN.json`; same shape regardless of source, so extract + load are source-agnostic.
- **Thread identity**: primary = Outlook `ConversationId`; fallback = subject normalization stripping English (`Re:` / `Fwd:` / `FW:`) and Greek (`Απ:` / `Πρ:`) prefixes.
- **Privacy**: `data/` is gitignored; `scripts/pii-gauntlet.sh` blocks personal data from tracked files — keep it green before every push.

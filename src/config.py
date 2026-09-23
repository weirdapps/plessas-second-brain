"""Configuration for the second-brain system.

Settings are loaded from environment variables. Identity and tenant settings
(BRAIN_* and SHAREPOINT_HOST) can also live in a per-host file, read below, so
that they reach entry points that start with an empty environment. Nothing
reads a .env file.
"""

import os
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path

# Keys the settings file may set: identity and tenant, named one by one. A
# credential, a backend switch (BRAIN_EXTRACT_ENGINE) or a relocated data home
# (BRAIN_DATA_DIR) pasted into it must not be picked up silently.
_CONFIG_FILE_KEYS = frozenset(
    {"BRAIN_USER_NAME", "BRAIN_USER_ROLE", "BRAIN_USER_EMAIL_PATTERN", "SHAREPOINT_HOST"}
)


def _config_value(raw: str) -> str:
    """The value part of a KEY=VALUE line: quotes removed, a # comment dropped.

    A comment starts at a "#" at the start of the value or after whitespace
    (a space or a tab), as in a shell; a "#" inside a word or quotes is kept.
    """
    value = raw.strip()
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing != -1:
            return value[1:closing]
    return re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()


def load_config_file(path: Path, environ: MutableMapping[str, str] = os.environ) -> None:
    """Apply KEY=VALUE lines from ``path`` to ``environ``; the environment wins.

    Claude Code starts the MCP server with an empty environment and systemd
    starts the timers with a fixed one, so a setting exported in a shell profile
    reaches neither. Before this existed, the producer ran with SHAREPOINT_HOST
    at its placeholder and every Mac MCP server ran without
    BRAIN_USER_EMAIL_PATTERN. Accepts ``export``, quotes and # comments. Runs at
    import of every entry point, so an unreadable file is reported and ignored,
    never fatal.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    except UnicodeDecodeError:
        print(f"second-brain: {path} is not UTF-8; ignoring it", file=sys.stderr)
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in _CONFIG_FILE_KEYS:
            try:
                environ.setdefault(key, _config_value(value))
            except ValueError:  # os.environ refuses an embedded NUL
                print(f"second-brain: skipping {key} in {path}: unusable value", file=sys.stderr)


load_config_file(
    Path(os.environ.get("BRAIN_CONFIG_FILE", str(Path.home() / ".config" / "second-brain" / "env")))
)

REPO_ROOT = Path(__file__).parent.parent
# Data root — override with BRAIN_DATA_DIR to point at a stable, checkout-independent
# location (e.g. ~/.second-brain/data). Everything under data/ derives from this.
DATA_ROOT = Path(os.environ.get("BRAIN_DATA_DIR", REPO_ROOT / "data"))
DEFAULT_DB = DATA_ROOT / "brain.db"

# User identity for extraction context and ownership detection.
# BRAIN_USER_EMAIL_PATTERN is used to detect your sent emails in stale thread
# detection (matched case-insensitively against sender_address).
USER_NAME = os.environ.get("BRAIN_USER_NAME", "")
USER_ROLE = os.environ.get("BRAIN_USER_ROLE", "")
USER_EMAIL_PATTERN = os.environ.get("BRAIN_USER_EMAIL_PATTERN", "")

# Extraction engine: "claude" (default) or "gemini"
EXTRACT_ENGINE = os.environ.get("BRAIN_EXTRACT_ENGINE", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_EXTRACT_MODEL") or os.environ.get(
    "VERTEX_MODEL_EXTRACT", "claude-sonnet-4-6"
)
GEMINI_MODEL = os.environ.get("BRAIN_GEMINI_MODEL", "gemini-2.5-flash")

# Schema version — bump when adding migrations (must match number of migrations in schema.py)
CURRENT_SCHEMA_VERSION = 20

ATTACHMENTS_DIR = DATA_ROOT / "attachments"
RAW_BATCH_DIR = DATA_ROOT / "raw"

# Conversation memory
CLAUDE_CODE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CONVERSATION_STAGING_DIR = DATA_ROOT / "staging" / "conversations"

# SharePoint reference attachments
SHAREPOINT_DATA_DIR = DATA_ROOT / "sharepoint"

# Wall-clock budget for the sync's inline-image classification step. Vision calls
# have unbounded latency (N images x LLM round-trip), so without a time box the
# step can consume a scheduler's entire TimeoutStartSec and be SIGTERMed. Kept
# well under the 30min systemd budget of sb-daily-sync / sb-noon-catchup.
IMAGE_CLASSIFY_BUDGET_S = float(os.environ.get("BRAIN_IMAGE_CLASSIFY_BUDGET_S", 480))

# News-reader SQLite database (external repo) — staged by `brain news-sync`.
NEWS_DB_PATH = Path(os.environ.get("BRAIN_NEWS_DB", Path.home() / "SourceCode/news/data/news.db"))

# Host we hold an interactive SharePoint session for (captured via
# `sharepoint-cli login --host <host>`). The session is only ever presented to
# this host and its <tenant>-my OneDrive twin; links to any other tenant are
# refused before a fetch. The default is a placeholder, so a host that fetches
# must set it (process-sharepoint refuses to run until it is set).
SHAREPOINT_HOST = os.environ.get("SHAREPOINT_HOST", "contoso.sharepoint.com")

# Document roots scanned by `brain reverse-ingest` and policed by
# scripts/health_check.py. Two layouts are live at once and neither can be
# hardcoded: on a Mac the trees sit under OneDrive's CloudStorage path, on the
# VPS they are a plain ~/Documents tree the laptop push job writes into.
# ~/Documents was a symlink into OneDrive until 2026-09-08; macOS then recreated
# it as an ordinary local folder, and it cannot be symlinked back because it
# carries the special-folder ACL "group:everyone deny delete". Candidate order
# mirrors scripts/wrappers/launchd/sync-documents-to-vps.sh, which already
# resolved this correctly while the Python side did not.
DOCUMENT_TREES = ("National", "Personal")
DOCUMENT_ROOT_CANDIDATES = (
    Path.home() / "Library/CloudStorage/OneDrive-Personal/Documents",
    Path.home() / "Documents",
)


def document_root_base() -> Path:
    """Base directory holding the document trees, resolved per host.

    Returns the first candidate that actually contains a tree; if none does,
    returns the last candidate so callers report a stable MISSING path rather
    than raising. Override with BRAIN_DOCUMENT_ROOT.
    """
    override = os.environ.get("BRAIN_DOCUMENT_ROOT")
    if override:
        return Path(override).expanduser()
    for base in DOCUMENT_ROOT_CANDIDATES:
        if any((base / tree).is_dir() for tree in DOCUMENT_TREES):
            return base
    return DOCUMENT_ROOT_CANDIDATES[-1]


def document_roots() -> list[Path]:
    """The document trees to scan, in a fixed order."""
    base = document_root_base()
    return [base / tree for tree in DOCUMENT_TREES]

"""Configuration for the second-brain system.

Settings are loaded from environment variables.
Set these in your shell profile or a .env file.
"""

import os
from pathlib import Path

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
# `sharepoint-cli login --host <host>`). Auth failures on this host
# mean the session expired and a re-login fixes them; auth failures on any
# other host are external tenants we cannot authenticate to, and are skipped
# rather than aborting a fetch pass.
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

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
CURRENT_SCHEMA_VERSION = 15

ATTACHMENTS_DIR = DATA_ROOT / "attachments"
RAW_BATCH_DIR = DATA_ROOT / "raw"

# Conversation memory
CLAUDE_CODE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CONVERSATION_STAGING_DIR = DATA_ROOT / "staging" / "conversations"

# SharePoint reference attachments
SHAREPOINT_DATA_DIR = DATA_ROOT / "sharepoint"

# Host we hold an interactive SharePoint session for (captured via
# `outlook-cli login --sharepoint-host <host>`). Auth failures on this host
# mean the session expired and a re-login fixes them; auth failures on any
# other host are external tenants we cannot authenticate to, and are skipped
# rather than aborting a fetch pass.
SHAREPOINT_HOST = os.environ.get("SHAREPOINT_HOST", "contoso.sharepoint.com")

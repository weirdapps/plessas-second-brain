"""Export Claude Code conversation history into second-brain staging format.

Parses JSONL session files from ~/.claude/projects/ and produces
structured conversation batches for extraction and loading.
"""

import json
from datetime import datetime
from pathlib import Path

from src.config import CLAUDE_CODE_PROJECTS_DIR, CONVERSATION_STAGING_DIR

# Record types that carry conversation content
_CONTENT_TYPES = {"user", "assistant"}

# Record types to skip entirely
_SKIP_TYPES = {"file-history-snapshot", "last-prompt", "progress", "queue-operation"}

# Minimum user turns to consider a conversation worth exporting
MIN_USER_TURNS = 2

# Maximum content length per turn (truncate very long tool outputs)
MAX_TURN_CONTENT = 50_000


def _workspace_from_dir_name(dir_name: str) -> str:
    """Convert project directory name back to a readable path.

    e.g. '-Users-you-Downloads' → '/Users/you/Downloads'
    """
    return "/" + dir_name.lstrip("-").replace("-", "/")


def _project_name_from_workspace(workspace: str) -> str:
    """Extract a short project name from a workspace path.

    e.g. '/Users/you/SourceCode/second-brain' → 'second-brain'
    """
    parts = workspace.rstrip("/").split("/")
    return parts[-1] if parts else workspace


def _extract_text_content(message: dict) -> str:
    """Extract readable text from a message's content field.

    Handles both string content (user messages) and list content
    (assistant messages with text/thinking/tool_use blocks).
    """
    content = message.get("content", "")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for block in content:
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
            elif block_type == "thinking":
                # Skip thinking blocks — internal reasoning
                pass
            elif block_type == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_input = block.get("input", {})
                # Summarize tool use compactly
                if tool_name in ("Read", "Glob", "Grep"):
                    target = (
                        tool_input.get("file_path")
                        or tool_input.get("pattern")
                        or tool_input.get("path", "")
                    )
                    parts.append(f"[Tool: {tool_name} {target}]")
                elif tool_name == "Edit":
                    parts.append(f"[Tool: Edit {tool_input.get('file_path', '')}]")
                elif tool_name == "Write":
                    parts.append(f"[Tool: Write {tool_input.get('file_path', '')}]")
                elif tool_name == "Bash":
                    cmd = tool_input.get("command", "")[:100]
                    parts.append(f"[Tool: Bash {cmd}]")
                else:
                    parts.append(f"[Tool: {tool_name}]")
            elif block_type == "tool_result":
                # Tool results appear in user messages — skip them
                pass
        return "\n".join(parts)

    return str(content)[:MAX_TURN_CONTENT]


def _has_code_blocks(text: str) -> bool:
    """Check if text contains markdown code blocks."""
    return "```" in text


def parse_conversation(jsonl_path: Path) -> dict | None:
    """Parse a single JSONL conversation file into structured format.

    Returns None if the conversation is too short or has no user content.
    """
    records = []
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (OSError, UnicodeDecodeError):
        return None

    if not records:
        return None

    # Extract metadata from first content record
    session_id = None
    workspace = None
    first_timestamp = None
    last_timestamp = None

    # Build turns by collapsing streaming chunks
    turns = []
    current_speaker = None
    current_parts = []
    current_timestamp = None
    current_has_tool = False

    for record in records:
        rec_type = record.get("type", "")

        if rec_type in _SKIP_TYPES:
            continue

        # Extract session metadata
        if not session_id and record.get("sessionId"):
            session_id = record["sessionId"]
        if not workspace and record.get("cwd"):
            workspace = record["cwd"]

        timestamp = record.get("timestamp")
        if timestamp:
            if not first_timestamp:
                first_timestamp = timestamp
            last_timestamp = timestamp

        if rec_type == "system":
            # System messages (hooks, etc.) — skip for turns
            continue

        if rec_type not in _CONTENT_TYPES:
            continue

        message = record.get("message", {})
        role = message.get("role", rec_type)
        speaker = "user" if role == "user" else "assistant"

        text = _extract_text_content(message)
        if not text:
            continue

        has_tool = "[Tool:" in text

        # Collapse consecutive messages from same speaker
        if speaker == current_speaker:
            current_parts.append(text)
            if has_tool:
                current_has_tool = True
            if timestamp:
                current_timestamp = timestamp
        else:
            # Flush previous turn
            if current_speaker and current_parts:
                combined = "\n".join(current_parts)
                if len(combined) > MAX_TURN_CONTENT:
                    combined = combined[:MAX_TURN_CONTENT] + "\n[...truncated]"
                turns.append(
                    {
                        "speaker": current_speaker,
                        "content": combined,
                        "timestamp": current_timestamp or "",
                        "has_code": _has_code_blocks(combined),
                        "has_tool_use": current_has_tool,
                    }
                )

            current_speaker = speaker
            current_parts = [text]
            current_timestamp = timestamp
            current_has_tool = has_tool

    # Flush last turn
    if current_speaker and current_parts:
        combined = "\n".join(current_parts)
        if len(combined) > MAX_TURN_CONTENT:
            combined = combined[:MAX_TURN_CONTENT] + "\n[...truncated]"
        turns.append(
            {
                "speaker": current_speaker,
                "content": combined,
                "timestamp": current_timestamp or "",
                "has_code": _has_code_blocks(combined),
                "has_tool_use": current_has_tool,
            }
        )

    # Filter: need minimum user turns
    user_turns = sum(1 for t in turns if t["speaker"] == "user")
    if user_turns < MIN_USER_TURNS:
        return None

    # Use file UUID as fallback session_id
    if not session_id:
        session_id = jsonl_path.stem

    return {
        "session_id": session_id,
        "file_name": jsonl_path.name,
        "workspace": workspace or "",
        "project_name": _project_name_from_workspace(workspace or ""),
        "started_at": first_timestamp or "",
        "ended_at": last_timestamp or "",
        "turn_count": len(turns),
        "turns": turns,
    }


def scan_conversation_files(
    days: int | None = None,
    workspace_filter: str | None = None,
) -> list[Path]:
    """Find all conversation JSONL files, optionally filtered.

    Args:
        days: Only include conversations modified in the last N days
        workspace_filter: Only include conversations from this workspace substring
    """
    if not CLAUDE_CODE_PROJECTS_DIR.exists():
        return []

    files = []
    cutoff = None
    if days is not None:
        cutoff = datetime.now().timestamp() - (days * 86400)

    for project_dir in CLAUDE_CODE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        # Apply workspace filter
        if workspace_filter:
            readable = _workspace_from_dir_name(project_dir.name)
            if workspace_filter.lower() not in readable.lower():
                continue

        for jsonl_file in project_dir.glob("*.jsonl"):
            if cutoff and jsonl_file.stat().st_mtime < cutoff:
                continue
            files.append(jsonl_file)

    # Sort by modification time (newest first for priority processing)
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files


def export_conversations(
    days: int | None = None,
    limit: int = 0,
    workspace_filter: str | None = None,
    exported_ids: set[str] | None = None,
) -> dict:
    """Export Claude Code conversations to staging directory.

    Args:
        days: Only export conversations from last N days (None = all)
        limit: Max conversations to export (0 = unlimited)
        workspace_filter: Only export from matching workspace
        exported_ids: Set of session_ids already exported (skip these)

    Returns:
        Dict with 'exported', 'skipped', 'errors', 'batch_file' keys
    """
    CONVERSATION_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    files = scan_conversation_files(days=days, workspace_filter=workspace_filter)
    if not files:
        return {"exported": 0, "skipped": 0, "errors": 0, "batch_file": None}

    exported_ids = exported_ids or set()
    conversations = []
    skipped = 0
    errors = 0

    for jsonl_file in files:
        if limit and len(conversations) >= limit:
            break

        try:
            conv = parse_conversation(jsonl_file)
        except Exception:
            errors += 1
            continue

        if conv is None:
            skipped += 1
            continue

        if conv["session_id"] in exported_ids:
            skipped += 1
            continue

        conversations.append(conv)

    if not conversations:
        return {"exported": 0, "skipped": skipped, "errors": errors, "batch_file": None}

    # Write batch file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_file = CONVERSATION_STAGING_DIR / f"conversation-batch-{timestamp}.json"
    with open(batch_file, "w", encoding="utf-8") as f:
        json.dump({"conversations": conversations}, f, ensure_ascii=False, indent=2)

    return {
        "exported": len(conversations),
        "skipped": skipped,
        "errors": errors,
        "batch_file": str(batch_file),
    }

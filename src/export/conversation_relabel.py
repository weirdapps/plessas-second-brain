"""Relabel the stored conversations by who wrote each turn.

The exporter used to store every user-role record as the owner's words. It now
reads Claude Code's own fields (src/export/conversation_export.py), but what it
stored before keeps the old labels until this runs once, on the producer:

    python -m src.cli relabel-conversations --dry-run   # counts, writes nothing
    python -m src.cli relabel-conversations

A stored owner turn that the owner did not write becomes speaker 'agent'. The
session's transcript decides where the producer still holds it and it lines up
with the stored copy; otherwise the turn's own text does. A conversation left
with no owner turn is an agent session from then on, and every conversation
read path leaves it out by default. Turns are relabelled, never split or
rewritten: a turn that joined the owner's words to a skill body keeps his label,
and nothing is deleted.
"""

import re
import sqlite3
from pathlib import Path

from src.export.conversation_export import (
    AGENT,
    ASSISTANT,
    HARNESS_OPENINGS,
    OWNER,
    is_agent_record,
    legacy_turns,
    read_records,
)
from src.redact import redact_secrets

# A harness block with its closing tag, which a stored turn may hold ahead of
# the owner's own words.
_HARNESS_BLOCK = re.compile(
    r"<(teammate-message|task-notification|system-reminder|cross-session-message|"
    r"command-name|command-message|command-args|local-command-[a-z]+|bash-input|"
    r"bash-stdout|bash-stderr)\b[^>]*>.*?</\1>",
    re.DOTALL,
)

# Characters of a stored turn compared with the transcript's to line the two up.
_OPENING = 80


def only_harness(text: str) -> bool:
    """Whether a stored turn holds nothing but what the harness put there, as far
    as its text can tell: once its tagged blocks are out, nothing is left, or
    what is left opens like the harness's own text."""
    rest = _HARNESS_BLOCK.sub("", text).strip()
    return not rest or rest.startswith(HARNESS_OPENINGS)


def _same_opening(stored: str, text: str) -> bool:
    """Whether a stored turn opens as the transcript's does; the stored copy was
    redacted on its way into staging."""
    head = stored[:_OPENING]
    return head == text[:_OPENING] or head == redact_secrets(text)[:_OPENING]


def _aligned(turns: list, legacy: list[tuple[str, str, bool]]) -> bool:
    """Whether the stored turns are the transcript's legacy turns as far as they
    go: the same roles in order, each user-role turn opening the same."""
    if not turns or len(turns) > len(legacy):
        return False
    for (_, speaker, content), (role, text, _) in zip(turns, legacy, strict=False):
        if (speaker == ASSISTANT) != (role == ASSISTANT):
            return False
        if role != ASSISTANT and not _same_opening(content or "", text):
            return False
    return True


def _not_typed(turns: list, transcript: Path | None) -> tuple[list[int], str]:
    """The ids of the stored owner turns the owner did not write, and what told:
    'transcript', 'misaligned' (the transcript is not the stored copy, so the text
    told) or 'text' (no transcript)."""
    owner = [(turn_id, content) for turn_id, speaker, content in turns if speaker == OWNER]
    records = read_records(transcript) if transcript else None
    method = "text"
    if records:
        if any(is_agent_record(r) for r in records):
            return [turn_id for turn_id, _ in owner], "transcript"
        legacy = legacy_turns(records)
        if _aligned(turns, legacy):
            flags = [typed for _, _, typed in legacy]
            return [
                turn_id
                for (turn_id, speaker, _), typed in zip(turns, flags, strict=False)
                if speaker == OWNER and not typed
            ], "transcript"
        method = "misaligned"
    return [turn_id for turn_id, content in owner if only_harness(content or "")], method


def relabel_conversations(
    conn: sqlite3.Connection, projects_dir: Path, *, dry_run: bool = False, batch: int = 500
) -> dict:
    """Demote the stored owner turns the owner did not write to speaker 'agent'.

    Args:
        conn: The store; opened read-only is enough for a dry run
        projects_dir: Where the transcripts are (<project>/<session>.jsonl)
        dry_run: Count only
        batch: Turns per write transaction, so the writers sharing the store wait
            seconds, not minutes

    Returns:
        Counts: conversations; by_transcript and by_text (how each was told) and
        misaligned (by text, though a transcript was there); turns_relabelled;
        agent_sessions_before and agent_sessions_after; agent_preferences, the
        [PREFERENCE] facts of the agent sessions after, which every read path now
        leaves out
    """
    transcripts = (
        {p.stem: p for p in projects_dir.glob("*/*.jsonl")} if projects_dir.is_dir() else {}
    )
    counts = dict.fromkeys(
        (
            "conversations",
            "by_transcript",
            "by_text",
            "misaligned",
            "turns_relabelled",
            "agent_sessions_before",
            "agent_sessions_after",
            "agent_preferences",
        ),
        0,
    )
    demote: list[int] = []
    for conversation_id, session_id in conn.execute(
        "SELECT id, session_id FROM conversations ORDER BY id"
    ).fetchall():
        turns = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, speaker, content FROM conversation_turns"
                " WHERE conversation_id = ? ORDER BY turn_index",
                (conversation_id,),
            )
        ]
        counts["conversations"] += 1
        owner_turns = sum(1 for _, speaker, _ in turns if speaker == OWNER)
        agent_turns = sum(1 for _, speaker, _ in turns if speaker == AGENT)
        # As the read paths tell one (src/store/conversation_query.py): no owner
        # turn, and some of another's.
        if not owner_turns and agent_turns:
            counts["agent_sessions_before"] += 1
        ids, method = _not_typed(turns, transcripts.get(session_id))
        counts["by_transcript" if method == "transcript" else "by_text"] += 1
        counts["misaligned"] += method == "misaligned"
        demote += ids
        if owner_turns == len(ids) and agent_turns + len(ids):
            counts["agent_sessions_after"] += 1
            counts["agent_preferences"] += conn.execute(
                "SELECT COUNT(*) FROM key_facts kf"
                " JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id"
                " WHERE ct.conversation_id = ? AND kf.fact LIKE '[PREFERENCE]%'",
                (conversation_id,),
            ).fetchone()[0]

    if dry_run:
        counts["turns_relabelled"] = len(demote)
        return counts
    size = max(1, batch)
    for start in range(0, len(demote), size):
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.executemany(
            "UPDATE conversation_turns SET speaker = ? WHERE id = ? AND speaker = ?",
            [(AGENT, turn_id, OWNER) for turn_id in demote[start : start + size]],
        )
        conn.commit()
        counts["turns_relabelled"] += cursor.rowcount
    return counts

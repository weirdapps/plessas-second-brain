"""brain whatsapp-sync, step 3: extract every dirty session, the Teams way.

For each thread with extraction_status in ('pending', 'failed'): build the
prompt from its messages, call the model through claude_extract.complete() (the
one policed route every call site uses), then replace the thread's decisions,
action items and key facts and mark it 'extracted'.

The model is never called inside a write transaction. Everything before the call
is a read; the thread's writes happen after it in a savepoint and are committed
at once, so a slow reply never holds the brain.db write lock that sb-teams-sync
and the other writers wait on.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime

from src.extract.policy_bridge import classify_exception
from src.extract.teams_pipeline import (
    MIN_SUBSTANTIVE_LENGTH,
    MIN_SUBSTANTIVE_MESSAGES,
    MIN_SUBSTANTIVE_TOTAL_CHARS,
)
from src.extract.vertex_auth import touch_sentinel
from src.extract.whatsapp_prompt import build_prompt, parse_response
from src.llm_policy import Outcome
from src.redact import redact_secrets


def _render(content: str | None, media_type: str | None, filename: str | None) -> str:
    """A message as the model sees it: its text, or its media kind in brackets."""
    text = (content or "").strip()
    if media_type:
        tag = f"[{media_type}{': ' + filename if filename else ''}]"
        return f"{tag} {text}".strip()
    return text


def extract_threads(
    conn: sqlite3.Connection, limit: int = 0, deadline_s: float | None = None
) -> dict:
    """Extract every dirty session; return {"extracted", "failed", "skipped", "deferred"}.

    `deadline_s` is a wall-clock budget: once spent no further thread is started
    and the rest stay pending for the next run, as in the Teams pipeline.
    """
    if not (os.environ.get("VERTEX_SDK_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")):
        raise RuntimeError(
            "VERTEX_SDK_PROJECT or ANTHROPIC_VERTEX_PROJECT_ID not set. "
            "Required for Vertex AI Claude extraction."
        )
    sql = "SELECT id FROM whatsapp_threads WHERE extraction_status IN ('pending','failed') ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    thread_ids = [r[0] for r in conn.execute(sql).fetchall()]

    counts = {"extracted": 0, "failed": 0, "skipped": 0, "deferred": 0}
    deadline = None if deadline_s is None else time.monotonic() + deadline_s
    for tid in thread_ids:
        if deadline is not None and time.monotonic() >= deadline:
            counts["deferred"] += 1
            continue
        counts[_extract_one(conn, tid)] += 1
        conn.commit()
    return counts


def _extract_one(conn: sqlite3.Connection, thread_id: int) -> str:
    thread = conn.execute(
        """
        SELECT t.started_at, t.ended_at, t.message_count, t.participant_names,
               c.name AS chat_name, c.chat_kind
        FROM whatsapp_threads t JOIN whatsapp_chats c ON c.id = t.chat_id
        WHERE t.id = ?
        """,
        (thread_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT sent_at, sender_name, content, media_type, filename FROM whatsapp_messages "
        "WHERE thread_id = ? ORDER BY sent_at",
        (thread_id,),
    ).fetchall()
    messages = [
        {
            "composed_at": r[0],
            "sender": r[1] or "(unknown)",
            "content": _render(r[2], r[3], r[4]),
        }
        for r in rows
    ]
    substantive = [m for m in messages if len(m["content"]) > MIN_SUBSTANTIVE_LENGTH]
    if (
        len(substantive) < MIN_SUBSTANTIVE_MESSAGES
        or sum(len(m["content"]) for m in substantive) < MIN_SUBSTANTIVE_TOTAL_CHARS
    ):
        conn.execute(
            "UPDATE whatsapp_threads SET extraction_status = 'skipped' WHERE id = ?", (thread_id,)
        )
        return "skipped"

    chat_label = thread[4] or ("group chat" if thread[5] == "group" else "chat")
    system_prompt, user_prompt = build_prompt(
        {
            "chat_label": chat_label,
            "started_at": thread[0],
            "ended_at": thread[1],
            "message_count": thread[2],
            "participants": json.loads(thread[3] or "[]"),
        },
        messages,
    )

    # Nothing uncommitted may be held across the call: see the module docstring.
    conn.commit()
    try:
        data = parse_response(_call_llm(system_prompt, user_prompt))
    except Exception as e:
        if classify_exception(e, None) is Outcome.AUTH_REAUTH_REQUIRED:
            touch_sentinel()
            conn.execute(
                "UPDATE whatsapp_threads SET extraction_status = 'pending', extraction_error = ? "
                "WHERE id = ?",
                (f"deferred (gcloud reauth needed): {str(e)[:400]}", thread_id),
            )
            return "deferred"
        conn.execute(
            "UPDATE whatsapp_threads SET extraction_status = 'failed', extraction_error = ? "
            "WHERE id = ?",
            (str(e)[:500], thread_id),
        )
        return "failed"

    conn.execute("SAVEPOINT whatsapp_extract")
    try:
        # The texts are masked as the loader masks an email's (src/store/loader.py).
        for table in ("decisions", "action_items", "key_facts"):
            conn.execute(f"DELETE FROM {table} WHERE whatsapp_thread_id = ?", (thread_id,))
        for d in data.get("decisions", []):
            conn.execute(
                "INSERT INTO decisions (decision, decided_by, decision_date, whatsapp_thread_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    redact_secrets(d.get("decision", "")),
                    d.get("decided_by"),
                    d.get("decision_date"),
                    thread_id,
                ),
            )
        for a in data.get("action_items", []):
            conn.execute(
                "INSERT INTO action_items (task, owner, deadline, status, whatsapp_thread_id) "
                "VALUES (?, ?, ?, 'open', ?)",
                (redact_secrets(a.get("task", "")), a.get("owner"), a.get("deadline"), thread_id),
            )
        for f in data.get("key_facts", []):
            conn.execute(
                "INSERT INTO key_facts (fact, whatsapp_thread_id) VALUES (?, ?)",
                (redact_secrets(f.get("fact", "")), thread_id),
            )
        first = next((m["content"] for m in substantive), "").replace("\n", " ")
        preview = first[:50] + ("…" if len(first) > 50 else "")
        conn.execute(
            """
            UPDATE whatsapp_threads
            SET title = ?, summary = ?, sentiment = ?, language = ?,
                extraction_status = 'extracted', extracted_at = ?, extraction_error = NULL
            WHERE id = ?
            """,
            (
                f"WhatsApp: {chat_label}: {preview}",
                data.get("summary", ""),
                data.get("sentiment"),
                data.get("language"),
                datetime.now(UTC).isoformat(),
                thread_id,
            ),
        )
        conn.execute("RELEASE whatsapp_extract")
    except Exception:
        conn.execute("ROLLBACK TO whatsapp_extract")
        conn.execute("RELEASE whatsapp_extract")
        raise
    return "extracted"


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """One extraction call through claude_extract.complete, the policed route.

    BRAIN_WHATSAPP_MODEL overrides the model; unset, complete() uses the
    configured one, as every other call site does.
    """
    from src.extract.claude_extract import _response_text, complete

    resp = complete(
        model=os.environ.get("BRAIN_WHATSAPP_MODEL") or None,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return _response_text(resp)

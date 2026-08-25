"""Step 5: LLM extraction over dirty threads.

For every thread with extraction_status IN ('pending','failed'):
  1. Build prompt over its messages.
  2. Call Vertex AI Claude.
  3. Parse JSON; clear prior decisions/actions/facts for this thread; insert new.
  4. Update thread row to extraction_status = 'extracted' (or 'failed' / 'skipped').

Re-extraction (when new messages land in an already-extracted thread) is
handled by deleting prior child rows BEFORE re-insert. The Step 6 embedder
detects extracted_at > embedding_at and re-embeds.
"""

import json
import sqlite3
import time
from datetime import UTC, datetime

from src.extract.policy_bridge import classify_exception
from src.extract.teams_prompt import build_prompt, parse_response
from src.extract.vertex_auth import touch_sentinel
from src.llm_policy import Outcome

# A "substantive" message is a non-system message above MIN_SUBSTANTIVE_LENGTH chars.
# Channel posts are commonly single-message announcements (chatsvcagg /posts returns
# top-level only — replies aren't fetched in Phase 1), so the rule is "at least one
# substantive message AND total content above the floor". A 200-char announcement
# passes; an "ok"+"lgtm" exchange (40 chars total) doesn't.
MIN_SUBSTANTIVE_MESSAGES = 1
MIN_SUBSTANTIVE_LENGTH = 20
MIN_SUBSTANTIVE_TOTAL_CHARS = 100


def extract_threads(
    conn: sqlite3.Connection,
    workers: int = 4,
    limit: int = 0,
    deadline_s: float | None = None,
) -> dict:
    """Extract structured knowledge for every dirty thread.

    Args:
        conn: open SQLite connection.
        workers: concurrency for LLM calls. Phase 1 implements sequential
            (workers param accepted for forward-compat).
        limit: max threads to process this run; 0 = unlimited.
        deadline_s: wall-clock budget. Once spent no further thread is STARTED
            and the rest come back as `deferred`. `limit` cannot stand in for
            this: the LLM policy allows up to 120 s per call, so even a dozen
            threads can outlast sb-teams-sync's TimeoutStartSec=600 on their
            own. Restoring the full chat corpus left 804 threads pending at
            once, which is what SIGTERMed the unit.

    Returns:
        {"extracted": <int>, "failed": <int>, "skipped": <int>}
    """
    # Fail fast on misconfiguration — better than burning every dirty thread
    # one by one with a KeyError marked as 'failed'.
    import os

    if not (os.environ.get("VERTEX_SDK_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")):
        raise RuntimeError(
            "VERTEX_SDK_PROJECT or ANTHROPIC_VERTEX_PROJECT_ID not set. "
            "Required for Vertex AI Claude extraction."
        )

    _ = workers  # parallelism is a Phase 1.5 follow-up if cost/throughput demands it
    sql = """
        SELECT id FROM teams_threads
        WHERE extraction_status IN ('pending','failed')
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    thread_ids = [r["id"] for r in conn.execute(sql).fetchall()]

    extracted = 0
    failed = 0
    skipped = 0
    deferred = 0

    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    for tid in thread_ids:
        # Checked before dispatch; the thread stays 'pending' for the next run.
        if deadline is not None and time.monotonic() >= deadline:
            deferred += 1
            continue
        outcome = _extract_one_thread(conn, tid)
        conn.commit()  # Per-thread checkpoint — survives crashes mid-run.
        if outcome == "extracted":
            extracted += 1
        elif outcome == "failed":
            failed += 1
        elif outcome == "skipped":
            skipped += 1
        elif outcome == "deferred":
            deferred += 1

    return {
        "extracted": extracted,
        "failed": failed,
        "skipped": skipped,
        "deferred": deferred,
    }


def _extract_one_thread(conn: sqlite3.Connection, thread_id: int) -> str:
    """Process one thread; return its outcome status."""
    thread = conn.execute(
        """
        SELECT t.*, c.team_name, c.topic AS chat_topic, c.chat_kind
        FROM teams_threads t
        JOIN teams_chats c ON c.id = t.chat_id
        WHERE t.id = ?
        """,
        (thread_id,),
    ).fetchone()

    messages = conn.execute(
        """
        SELECT composed_at, sender_display_name, content_text, is_system
        FROM teams_messages
        WHERE thread_id = ?
        ORDER BY composed_at
        """,
        (thread_id,),
    ).fetchall()

    substantive = [
        m
        for m in messages
        if not m["is_system"] and len(m["content_text"] or "") > MIN_SUBSTANTIVE_LENGTH
    ]
    total_chars = sum(len(m["content_text"] or "") for m in substantive)
    if len(substantive) < MIN_SUBSTANTIVE_MESSAGES or total_chars < MIN_SUBSTANTIVE_TOTAL_CHARS:
        conn.execute(
            "UPDATE teams_threads SET extraction_status='skipped' WHERE id = ?",
            (thread_id,),
        )
        return "skipped"

    prompt_thread = {
        "chat_label": f"{thread['team_name'] or '?'} / {thread['chat_topic'] or '?'}",
        "thread_kind": thread["thread_kind"],
        "started_at": thread["started_at"],
        "ended_at": thread["ended_at"],
        "message_count": thread["message_count"],
        "participants": json.loads(thread["participant_display_names"] or "[]"),
    }
    prompt_messages = [
        {
            "composed_at": m["composed_at"],
            "sender": m["sender_display_name"] or "(unknown)",
            "content": m["content_text"] or "",
        }
        for m in substantive
    ]
    system_prompt, user_prompt = build_prompt(prompt_thread, prompt_messages)

    try:
        raw = _call_llm(system_prompt, user_prompt)
        data = parse_response(raw)
    except Exception as e:
        # The classifier, not a pattern list. is_vertex_auth_error matches on the message,
        # and two of the three types the policy calls re-authable —
        # anthropic.AuthenticationError and PermissionDeniedError — carry nothing in theirs
        # to match. Those were written 'failed', and extract_threads selects
        # ('pending','failed'), so unlike attachments the row WAS still picked up next run.
        # The cost was the label rather than the retry: a re-authable outage read as a hard
        # failure in the stats and raised no sentinel, so nothing told the estate a re-auth
        # was owed. Same fix, same classifier, as attachment_pipeline.
        if classify_exception(e, None) is Outcome.AUTH_REAUTH_REQUIRED:
            # Vertex ADC expired. Mark pending so the next cron retries
            # automatically once the user re-auths. See vertex_auth.py.
            touch_sentinel()
            conn.execute(
                "UPDATE teams_threads SET extraction_status='pending', "
                "extraction_error=? WHERE id = ?",
                (
                    f"deferred (gcloud reauth needed): {str(e)[:400]}",
                    thread_id,
                ),
            )
            return "deferred"
        conn.execute(
            "UPDATE teams_threads SET extraction_status='failed', extraction_error=? WHERE id = ?",
            (str(e)[:500], thread_id),
        )
        return "failed"

    conn.execute("SAVEPOINT thread_extract")
    try:
        # Clear prior child rows from any earlier extraction.
        for table in ("decisions", "action_items", "key_facts"):
            conn.execute(f"DELETE FROM {table} WHERE teams_thread_id = ?", (thread_id,))

        for d in data.get("decisions", []):
            conn.execute(
                "INSERT INTO decisions(decision, decided_by, decision_date, teams_thread_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    d.get("decision", ""),
                    d.get("decided_by"),
                    d.get("decision_date"),
                    thread_id,
                ),
            )
        for a in data.get("action_items", []):
            conn.execute(
                "INSERT INTO action_items(task, owner, deadline, status, teams_thread_id) "
                "VALUES (?, ?, ?, 'open', ?)",
                (a.get("task", ""), a.get("owner"), a.get("deadline"), thread_id),
            )
        for f in data.get("key_facts", []):
            conn.execute(
                "INSERT INTO key_facts(fact, teams_thread_id) VALUES (?, ?)",
                (f.get("fact", ""), thread_id),
            )

        title = _generate_title(prompt_thread, prompt_messages)
        conn.execute(
            """
            UPDATE teams_threads
            SET title = ?, summary = ?, sentiment = ?, language = ?,
                extraction_status = 'extracted', extracted_at = ?,
                extraction_error = NULL
            WHERE id = ?
            """,
            (
                title,
                data.get("summary", ""),
                data.get("sentiment"),
                data.get("language"),
                datetime.now(UTC).isoformat(),
                thread_id,
            ),
        )
        conn.execute("RELEASE thread_extract")
    except Exception:
        conn.execute("ROLLBACK TO thread_extract")
        conn.execute("RELEASE thread_extract")
        raise

    return "extracted"


def _generate_title(thread: dict, messages: list[dict]) -> str:
    """Cheap human-readable title."""
    if not messages:
        return f"{thread['chat_label']} thread"
    first = (messages[0]["content"] or "").strip().replace("\n", " ")
    preview = (first[:50] + "…") if len(first) > 50 else first
    return f"{thread['chat_label']}: {preview}"


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """Invoke Vertex AI Claude via the shared extraction client, under the retry policy.

    Uses the process-wide shared client from claude_extract (built once, reused,
    never closed per call) so Teams extraction does not fork `gcloud config get
    project` once per thread. Teams keeps its own model override; model is a
    per-request arg, so one project/region-scoped client serves any model.

    THIS WAS THE SIXTH CALL SITE AND THE ONLY UNPOLICED ONE. It called the SDK directly,
    so Teams extraction had no retry, no re-auth and no deadline at all: a single 429 or a
    stale ADC lost the thread outright, on a 600s unit. Worse in one specific way — it
    already shared the client that ``reset_client_cache`` invalidates, so it benefited from
    a re-auth triggered by some other pipeline in the same process while being structurally
    incapable of triggering one itself. One unpoliced site reproduces in miniature the
    divergence this whole port exists to end.

    ``_do_call`` re-fetches the client on every attempt, exactly as extract_one,
    attachment_pipeline, image_vision and calendar_extractor do, so a successful reauth
    (which calls reset_client_cache) is picked up by the retry rather than silently reusing
    the dead credential.

    Returns the raw text response.
    """
    import os

    from src.extract.claude_extract import (
        _get_client_and_model,
        _response_text,
        call_with_policy,
    )
    from src.extract.vertex_fallback import create_with_refusal_fallback

    model = (
        os.environ.get("BRAIN_TEAMS_MODEL")
        or os.environ.get("VERTEX_MODEL_EXTRACT")
        or "claude-sonnet-4-6"
    )

    def _do_call():
        # Shared client — do not close it here (see claude_extract._get_client_and_model).
        client, _ = _get_client_and_model()
        return create_with_refusal_fallback(
            client,
            model=model,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

    resp = call_with_policy(_do_call, max_call_seconds=120.0)
    # Same leading-ThinkingBlock hazard as every other call site. No teams_threads row
    # has hit it yet (extraction_error is NULL on all 6008), so this one is preventive:
    # the sixth site should not be the one left indexing position 0.
    return _response_text(resp)

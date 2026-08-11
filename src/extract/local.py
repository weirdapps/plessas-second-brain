"""
Local streaming extraction using Gemini 2.5 Flash or Claude Haiku.

Concurrent processing with ThreadPoolExecutor.
Resumable state tracking.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from src.config import DATA_ROOT

# Repo root
REPO_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = DATA_ROOT
STAGING_DIR = DATA_DIR / "staging"
EXTRACTED_DIR = DATA_DIR / "extracted"
STATE_FILE = DATA_DIR / "state" / "extract_state.json"
LOG_FILE = DATA_DIR / "extract.log"

GEMINI_MODEL = "gemini-2.5-flash"
SAVE_INTERVAL = 50
CALL_TIMEOUT = 60  # seconds per API call
DEFAULT_ENGINE = os.environ.get("BRAIN_EXTRACT_ENGINE", "claude")  # "gemini" or "claude"
CONSECUTIVE_FAIL_THRESHOLD = 5  # pause after this many consecutive failures
QUOTA_PAUSE_SECONDS = 3600  # 1 hour default pause when quota exhausted

_shutdown = False
_state_lock = threading.Lock()
_log_lock = threading.Lock()


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": [], "total_extracted": 0, "failures": 0}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def collect_emails() -> list[dict]:
    batch_files = sorted(STAGING_DIR.glob("batch-*.json"))
    all_emails = []
    for bf in batch_files:
        data = json.load(open(bf, encoding="utf-8"))
        emails = data.get("emails", []) if isinstance(data, dict) else data
        all_emails.extend(emails)
    return all_emails


def extract_one(email: dict, api_key: str | None, engine: str = "gemini") -> dict | None:
    """Extract a single email using the specified engine."""
    if engine == "claude":
        from src.extract.claude_extract import extract_one as claude_extract

        return claude_extract(email)

    from google import genai
    from google.genai import types

    sys.path.insert(0, str(REPO_ROOT))
    from src.extract.parser import parse_extraction
    from src.extract.prompt import build_extraction_prompt

    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    prompt = build_extraction_prompt(email)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0)),
    )

    text = response.text
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])

    extraction = parse_extraction(text)
    extraction["message_id"] = email["message_id"]
    return extraction


def extract_with_timeout(
    email: dict, api_key: str | None, timeout: int = CALL_TIMEOUT
) -> tuple[str, dict | None]:
    """Run extraction in a subprocess with hard timeout."""
    msg_id = str(email.get("message_id", "unknown"))

    # Write email to temp file for subprocess
    tmp_in = DATA_DIR / "state" / f"_tmp_email_{os.getpid()}.json"
    tmp_out = DATA_DIR / "state" / f"_tmp_result_{os.getpid()}.json"

    try:
        tmp_in.write_text(json.dumps(email, ensure_ascii=False))

        env = os.environ.copy()
        if api_key:
            env["GEMINI_API_KEY"] = api_key

        # Run extraction in subprocess
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"""
import json, sys, os
sys.path.insert(0, {str(REPO_ROOT)!r})
from src.extract.local import extract_one

email = json.loads(open({str(tmp_in)!r}).read())
api_key = os.environ.get("GEMINI_API_KEY")
result = extract_one(email, api_key)
open({str(tmp_out)!r}, "w").write(json.dumps(result, ensure_ascii=False))
""",
            ],
            timeout=timeout,
            capture_output=True,
            text=True,
            env=env,
        )

        if result.returncode == 0 and tmp_out.exists():
            extraction = json.loads(tmp_out.read_text())
            return (msg_id, extraction)
        else:
            return (msg_id, None)

    except subprocess.TimeoutExpired:
        return (msg_id, None)
    except Exception:
        return (msg_id, None)
    finally:
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)


def _parse_retry_delay(exc: Exception) -> int | None:
    """Extract retry delay seconds from a 429 quota error, or None."""
    msg = str(exc)
    if "429" not in msg and "RESOURCE_EXHAUSTED" not in msg:
        return None
    # Look for "retry in XhYmZs" pattern
    import re

    m = re.search(r"retry\s+in\s+(\d+)h(\d+)m", msg, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    m = re.search(r"retryDelay.*?(\d+)s", msg)
    if m:
        return int(m.group(1))
    return 3600  # default 1h if we can't parse


def _should_quota_pause(exc: Exception) -> bool:
    """Return True for genuine quota exhaustion, False for auth and other errors.

    Delegates to classify_exception, which now has a secondary rate-limit string
    widener matching the auth widener's shape.  An auth exception cannot reach
    RATE_LIMIT because type checks run before either string widener.
    """
    from src.extract.policy_bridge import classify_exception
    from src.llm_policy import Outcome

    return classify_exception(exc, None) is Outcome.RATE_LIMIT


def extract_inline(
    email: dict, api_key: str | None, max_retries: int = 3, engine: str = "gemini"
) -> tuple[str, dict | None, bool]:
    """Extract inline with retries. Thread-safe for Claude engine.

    Returns (msg_id, extraction_or_None, is_quota_error).
    The caller uses is_quota_error to trigger a global pause.

    For Gemini: uses SIGALRM-based timeout (main thread only).
    For Claude: relies on SDK's built-in HTTP timeout (no SIGALRM).
    """
    global _shutdown
    msg_id = str(email.get("message_id", "unknown"))
    is_quota = False
    use_alarm = (engine != "claude") and threading.current_thread() is threading.main_thread()

    for attempt in range(max_retries):
        try:
            if use_alarm:
                signal.signal(signal.SIGALRM, lambda s, f: (_ for _ in ()).throw(TimeoutError()))
                signal.alarm(CALL_TIMEOUT)

            result = extract_one(email, api_key, engine=engine)

            if use_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, _handle_signal)
            return (msg_id, result, False)

        except TimeoutError:
            if use_alarm:
                signal.alarm(0)
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                return (msg_id, None, False)

        except Exception as e:
            if use_alarm:
                signal.alarm(0)
            if _should_quota_pause(e):
                is_quota = True
                if attempt < max_retries - 1:
                    log(
                        f"Quota error on msg {msg_id} (attempt {attempt + 1}/{max_retries}), brief wait..."
                    )
                    time.sleep(min(30, 2 ** (attempt + 1)))
                    continue
                else:
                    return (msg_id, None, True)
            from src.extract.policy_bridge import classify_exception
            from src.llm_policy import Outcome

            if classify_exception(e, None) is Outcome.AUTH_REAUTH_REQUIRED:
                from src.extract.vertex_auth import touch_sentinel

                log(
                    f"Auth failure on msg {msg_id}: credential expired; "
                    "writing reauth sentinel and stopping."
                )
                touch_sentinel()
                _shutdown = True
                return (msg_id, None, False)
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                log(f"  ↳ msg {msg_id} error: {type(e).__name__}: {str(e)[:200]}")
                return (msg_id, None, is_quota)

    return (msg_id, None, is_quota)


def _worker_fn(email: dict, api_key: str | None, engine: str) -> tuple[str, dict | None, bool]:
    """Worker function for ThreadPoolExecutor."""
    return extract_inline(email, api_key, engine=engine)


def run_extraction(workers: int = 1, limit: int = 0, engine: str | None = None):
    global _shutdown

    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)

    engine = engine or DEFAULT_ENGINE
    api_key = os.environ.get("GEMINI_API_KEY")

    log("=== LOCAL EXTRACTION STARTED ===")
    log(f"Engine: {engine}, Workers: {workers}, Timeout: {CALL_TIMEOUT}s")

    state = load_state()
    processed_ids = set(state.get("processed_ids", []))
    log(f"Previously extracted: {len(processed_ids)} emails")

    log("Loading staged emails...")
    all_emails = collect_emails()
    log(f"Total staged emails: {len(all_emails)}")

    pending = [e for e in all_emails if str(e.get("message_id", "")) not in processed_ids]
    log(f"Pending extraction: {len(pending)} emails")

    if limit > 0:
        pending = pending[:limit]
        log(f"Limited to: {limit} emails")

    if not pending:
        log("Nothing to extract. Done.")
        return

    # Warm up: verify auth
    if engine == "claude":
        from src.extract.claude_extract import _get_client_and_model

        client, model_name = _get_client_and_model()
        del client
        log(f"Claude auth OK (model: {model_name})")
    else:
        from google import genai

        client = genai.Client(api_key=api_key) if api_key else genai.Client()
        del client
        log(f"Gemini auth OK (model: {GEMINI_MODEL})")

    total_done = 0
    total_failed = 0
    start_time = time.time()
    unsaved_count = 0
    consecutive_failures = 0

    if workers <= 1:
        # Sequential mode (original behavior)
        i = 0
        while i < len(pending):
            if _shutdown:
                log("Shutdown requested, saving state...")
                break

            email = pending[i]
            msg_id, extraction, is_quota = extract_inline(email, api_key, engine=engine)

            if extraction is not None:
                result_file = EXTRACTED_DIR / f"{msg_id}.json"
                with open(result_file, "w") as f:
                    json.dump(extraction, f, indent=2, ensure_ascii=False)
                processed_ids.add(msg_id)
                total_done += 1
                consecutive_failures = 0
                i += 1
            else:
                total_failed += 1
                if is_quota:
                    consecutive_failures += 1
                log(f"FAILED msg {msg_id}")

                if consecutive_failures >= CONSECUTIVE_FAIL_THRESHOLD:
                    pause = QUOTA_PAUSE_SECONDS
                    log(
                        f"QUOTA PAUSE: {consecutive_failures} consecutive failures. "
                        f"Sleeping {pause // 3600}h{(pause % 3600) // 60}m until quota resets..."
                    )
                    state["processed_ids"] = list(processed_ids)
                    state["total_extracted"] = len(processed_ids)
                    state["failures"] = total_failed
                    save_state(state)

                    sleep_end = time.time() + pause
                    while time.time() < sleep_end and not _shutdown:
                        time.sleep(min(60, sleep_end - time.time()))

                    if _shutdown:
                        log("Shutdown requested during quota pause.")
                        break

                    consecutive_failures = 0
                    i = max(0, i - CONSECUTIVE_FAIL_THRESHOLD + 1)
                    log(f"Resuming extraction from msg index {i}...")
                    continue

                i += 1

            unsaved_count += 1

            if unsaved_count >= SAVE_INTERVAL:
                state["processed_ids"] = list(processed_ids)
                state["total_extracted"] = len(processed_ids)
                state["failures"] = total_failed
                save_state(state)

                elapsed = (time.time() - start_time) / 60
                rate = total_done / elapsed if elapsed > 0 else 0
                remaining = len(pending) - i
                eta = remaining / rate if rate > 0 else 0
                log(
                    f"Progress: {total_done}/{len(pending)} extracted, "
                    f"{total_failed} failures, "
                    f"{elapsed:.1f}min elapsed, "
                    f"{rate:.1f} emails/min, "
                    f"ETA: {eta:.0f}min"
                )
                unsaved_count = 0
    else:
        # Concurrent mode with ThreadPoolExecutor
        log(f"Starting concurrent extraction with {workers} workers...")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Submit work in chunks to allow shutdown checks
            chunk_size = workers * 10
            i = 0

            while i < len(pending) and not _shutdown:
                chunk = pending[i : i + chunk_size]
                futures = {
                    executor.submit(_worker_fn, email, api_key, engine): email for email in chunk
                }

                for future in as_completed(futures):
                    if _shutdown:
                        break

                    msg_id, extraction, is_quota = future.result()

                    if extraction is not None:
                        result_file = EXTRACTED_DIR / f"{msg_id}.json"
                        with open(result_file, "w") as f:
                            json.dump(extraction, f, indent=2, ensure_ascii=False)
                        with _state_lock:
                            processed_ids.add(msg_id)
                            total_done += 1
                            consecutive_failures = 0
                    else:
                        with _state_lock:
                            total_failed += 1
                            if is_quota:
                                consecutive_failures += 1
                        log(f"FAILED msg {msg_id}")

                    with _state_lock:
                        unsaved_count += 1

                    if unsaved_count >= SAVE_INTERVAL:
                        with _state_lock:
                            state["processed_ids"] = list(processed_ids)
                            state["total_extracted"] = len(processed_ids)
                            state["failures"] = total_failed
                            save_state(state)
                            unsaved_count = 0

                        elapsed = (time.time() - start_time) / 60
                        rate = total_done / elapsed if elapsed > 0 else 0
                        remaining = len(pending) - (i + len(chunk))
                        eta = remaining / rate if rate > 0 else 0
                        log(
                            f"Progress: {total_done}/{len(pending)} extracted, "
                            f"{total_failed} failures, "
                            f"{elapsed:.1f}min elapsed, "
                            f"{rate:.1f} emails/min, "
                            f"ETA: {eta:.0f}min"
                        )

                # Quota pause check between chunks
                if consecutive_failures >= CONSECUTIVE_FAIL_THRESHOLD:
                    pause = QUOTA_PAUSE_SECONDS
                    log(
                        f"QUOTA PAUSE: {consecutive_failures} consecutive failures. "
                        f"Sleeping {pause // 3600}h{(pause % 3600) // 60}m..."
                    )
                    with _state_lock:
                        state["processed_ids"] = list(processed_ids)
                        state["total_extracted"] = len(processed_ids)
                        state["failures"] = total_failed
                        save_state(state)

                    sleep_end = time.time() + pause
                    while time.time() < sleep_end and not _shutdown:
                        time.sleep(min(60, sleep_end - time.time()))

                    if _shutdown:
                        log("Shutdown requested during quota pause.")
                        break
                    consecutive_failures = 0

                i += chunk_size

    # Final save
    state["processed_ids"] = list(processed_ids)
    state["total_extracted"] = len(processed_ids)
    state["failures"] = total_failed
    save_state(state)

    elapsed = (time.time() - start_time) / 60
    log(f"=== EXTRACTION {'STOPPED' if _shutdown else 'COMPLETE'} ===")
    log(f"Extracted: {total_done}, Failed: {total_failed}, Time: {elapsed:.1f}min")


# --- Conversation Extraction ---

CONV_STAGING_DIR = DATA_DIR / "staging" / "conversations"
CONV_EXTRACTED_DIR = DATA_DIR / "extracted" / "conversations"
CONV_STATE_FILE = DATA_DIR / "state" / "conv_extract_state.json"


def collect_conversations() -> list[dict]:
    """Load all staged conversation batches."""
    batch_files = sorted(CONV_STAGING_DIR.glob("conversation-batch-*.json"))
    all_convs = []
    for bf in batch_files:
        data = json.load(open(bf, encoding="utf-8"))
        convs = data.get("conversations", [])
        all_convs.extend(convs)
    return all_convs


def extract_conversation_inline(
    conversation: dict,
    max_retries: int = 3,
) -> tuple[str, dict | None, bool]:
    """Extract a single conversation with retries. Returns (session_id, extraction, is_quota)."""
    session_id = conversation.get("session_id", "unknown")
    is_quota = False

    for attempt in range(max_retries):
        try:
            from src.extract.claude_extract import extract_conversation

            result = extract_conversation(conversation)
            return (session_id, result, False)

        except Exception as e:
            retry_delay = _parse_retry_delay(e)
            if retry_delay is not None:
                is_quota = True
                if attempt < max_retries - 1:
                    log(
                        f"Quota error on conv {session_id[:12]} (attempt {attempt + 1}), waiting..."
                    )
                    time.sleep(min(30, 2 ** (attempt + 1)))
                    continue
                return (session_id, None, True)

            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                log(f"FAILED conv {session_id[:12]}: {e}")
                return (session_id, None, is_quota)

    return (session_id, None, is_quota)


def run_conversation_extraction(workers: int = 1, limit: int = 0):
    """Extract structured data from staged conversations.

    Reuses the same concurrency, quota handling, and state tracking
    patterns as email extraction.
    """
    global _shutdown

    CONV_EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)

    log("=== CONVERSATION EXTRACTION STARTED ===")
    log(f"Workers: {workers}")

    # Load state
    conv_state = {}
    if CONV_STATE_FILE.exists():
        conv_state = json.loads(CONV_STATE_FILE.read_text())
    processed_ids = set(conv_state.get("processed_ids", []))
    log(f"Previously extracted: {len(processed_ids)} conversations")

    # Load staged conversations
    all_convs = collect_conversations()
    log(f"Total staged conversations: {len(all_convs)}")

    pending = [c for c in all_convs if c.get("session_id", "") not in processed_ids]
    log(f"Pending extraction: {len(pending)} conversations")

    if limit > 0:
        pending = pending[:limit]
        log(f"Limited to: {limit}")

    if not pending:
        log("Nothing to extract. Done.")
        return

    # Verify auth
    from src.extract.claude_extract import _get_client_and_model

    client, model_name = _get_client_and_model()
    del client
    log(f"Claude auth OK (model: {model_name})")

    total_done = 0
    total_failed = 0
    start_time = time.time()

    for i, conv in enumerate(pending):
        if _shutdown:
            log("Shutdown requested, saving state...")
            break

        session_id, extraction, is_quota = extract_conversation_inline(conv)

        if extraction is not None:
            result_file = CONV_EXTRACTED_DIR / f"{session_id}.json"
            with open(result_file, "w") as f:
                json.dump(extraction, f, indent=2, ensure_ascii=False)
            processed_ids.add(session_id)
            total_done += 1
            log(f"Extracted conv {session_id[:12]}... ({i + 1}/{len(pending)})")
        else:
            total_failed += 1
            log(f"FAILED conv {session_id[:12]}...")

        # Save state periodically
        if (i + 1) % SAVE_INTERVAL == 0 or i == len(pending) - 1:
            conv_state["processed_ids"] = list(processed_ids)
            conv_state["total_extracted"] = len(processed_ids)
            conv_state["failures"] = total_failed
            conv_state["last_updated"] = datetime.now().isoformat()
            CONV_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONV_STATE_FILE.write_text(json.dumps(conv_state, indent=2))

            elapsed = (time.time() - start_time) / 60
            rate = total_done / elapsed if elapsed > 0 else 0
            log(f"Progress: {total_done}/{len(pending)}, {elapsed:.1f}min, {rate:.1f}/min")

    # Final save
    conv_state["processed_ids"] = list(processed_ids)
    conv_state["total_extracted"] = len(processed_ids)
    conv_state["failures"] = total_failed
    conv_state["last_updated"] = datetime.now().isoformat()
    CONV_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONV_STATE_FILE.write_text(json.dumps(conv_state, indent=2))

    elapsed = (time.time() - start_time) / 60
    log(f"=== CONVERSATION EXTRACTION {'STOPPED' if _shutdown else 'COMPLETE'} ===")
    log(f"Extracted: {total_done}, Failed: {total_failed}, Time: {elapsed:.1f}min")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Local streaming extraction")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent workers")
    parser.add_argument("--limit", type=int, default=0, help="Max emails to process (default: all)")
    args = parser.parse_args()

    run_extraction(workers=args.workers, limit=args.limit)


if __name__ == "__main__":
    main()

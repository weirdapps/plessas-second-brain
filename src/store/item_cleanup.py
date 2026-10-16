"""One-off clean-ups of the extracted items, run by hand on the producer (`brain maint ...`).

Each is a dry run unless given --apply, and refuses to run on a replica (src/cli.py).

forget-itemless
    Deletes the decisions, action items and commitments whose email is news or the owner's
    automation mail (source_class.ITEMLESS_CLASSES), attachment-written ones included. The
    loader takes none from them any more (src/store/loader.py); what earlier code took stays
    until this runs: on the replica of 2026-10-16, 44,627 decisions (19,606 from news,
    written before news extraction stopped asking the model on 24 Sep, and 25,021 from
    automation), 23,190 actions and 5,113 commitments. Key facts and topics stay.

mark-boilerplate
    Marks as boilerplate the key facts whose text recurs in --min-threads (default 5) or more
    distinct threads: encryption notices, sign-offs, news tags, a daily report's fixed lines
    (74K rows on the replica of 2026-10-16). Text is compared folded (fact_text): accents,
    case and punctuation aside. A marked row gets key_facts.boilerplate = 1 and its text's key
    goes in boilerplate_fact_keys, so the loader marks a later copy as it stores it
    (is_boilerplate). Nothing is deleted. Thread: an email's conversation_id (else the email),
    a Teams or WhatsApp thread, a Claude Code conversation.

reextract-stacked
    Extracts again, in the configured call shape (v2-text by default), the body of each email
    that holds key facts no attachment wrote (attachment_id NULL) beside facts an attachment
    wrote: 6,318 emails on the replica, with 164,299 untagged facts. Until schema v28 an
    attachment's facts carried no attachment_id, and a later summary of the attachment, which
    deletes only its tagged rows, left them beside the new ones. The untagged rows mix those
    stale generations with the body's own facts, and nothing in them tells the two apart (v1
    wrote English facts for Greek mail, so not even their words), so they are never deleted
    on attachment_id IS NULL alone: an email's untagged facts, decisions and action items and
    its commitments are replaced only by a new extraction of that email's body, in one
    transaction (loader.replace_items). Its summary, topics, people and vector stay as they
    are. Each email done or failed is recorded in sync_metadata as
    stacked_reextract:<email id>, so a run resumes where the last stopped; an email that
    failed three times is not offered again. --token-budget bounds a run (required with
    --apply); --limit bounds the emails it takes; the dry run prints the calls, tokens and
    cost. Stops at a quota, timeout or credential failure, leaving the rest for the next run.
"""

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from src.store.source_class import ITEMLESS_CLASSES, _require_column

# --- boilerplate ----------------------------------------------------------------------------

DEFAULT_MIN_THREADS = 5
_NOT_WORD = re.compile(r"[\W_]+")


def fact_text(text: str | None) -> str:
    """A fact's text as it is compared for boilerplate: accents dropped, case folded, final
    sigma as sigma, punctuation as space, spacing collapsed."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NOT_WORD.sub(" ", bare.casefold().replace("ς", "σ")).split())


def fact_key(text: str | None) -> str | None:
    """The key of a fact's folded text in boilerplate_fact_keys; None for text with no
    letter or digit, which is never boilerplate."""
    folded = fact_text(text)
    return hashlib.sha256(folded.encode()).hexdigest()[:16] if folded else None


def is_boilerplate(conn: sqlite3.Connection, fact: str | None) -> bool:
    """Whether the folded text of `fact` is marked boilerplate; False on a store older than
    v35, which has no keys."""
    key = fact_key(fact)
    if key is None:
        return False
    try:
        row = conn.execute("SELECT 1 FROM boilerplate_fact_keys WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return False
        raise
    return row is not None


# A fact's thread: its email's conversation, else the email; its Teams or WhatsApp thread;
# its conversation; for anything else the row alone, which shares no thread.
_THREAD_SQL = """CASE
    WHEN kf.email_id IS NOT NULL THEN COALESCE(t.thread, 'e:' || kf.email_id)
    WHEN kf.teams_thread_id IS NOT NULL THEN 't:' || kf.teams_thread_id
    WHEN kf.whatsapp_thread_id IS NOT NULL THEN 'w:' || kf.whatsapp_thread_id
    WHEN kf.conversation_turn_id IS NOT NULL
        THEN 'c:' || COALESCE(ct.conversation_id, 'turn' || kf.conversation_turn_id)
    ELSE 'f:' || kf.id
END"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def mark_boilerplate(
    conn: sqlite3.Connection,
    min_threads: int = DEFAULT_MIN_THREADS,
    *,
    apply: bool = False,
    show: int = 10,
) -> dict:
    """Find the fact texts found in `min_threads` or more distinct threads; with `apply`,
    mark their rows and record their keys. Returns the counts, and the `show` commonest
    texts with how many threads hold each.

    The work is in temporary tables, so a dry run writes nothing to the store. The email
    threads are read from idx_emails_conversation_id alone: conversation_id sits behind the
    body, which a read of the table would page through for every email.
    """
    if min_threads < 2:
        raise ValueError("min_threads must be 2 or more: one thread is not a repeat")
    conn.create_function("sb_fact_key", 1, fact_key, deterministic=True)
    conn.executescript(
        """
        DROP TABLE IF EXISTS temp.bp_email_thread;
        DROP TABLE IF EXISTS temp.bp_fact;
        DROP TABLE IF EXISTS temp.bp_keys;
        CREATE TEMP TABLE bp_email_thread (id INTEGER PRIMARY KEY, thread TEXT);
        CREATE TEMP TABLE bp_fact (id INTEGER PRIMARY KEY, key TEXT, thread TEXT);
        """
    )
    conn.execute(
        "INSERT INTO temp.bp_email_thread (id, thread) SELECT id,"
        " 'e:' || COALESCE(NULLIF(conversation_id, ''), 'id' || id) FROM emails"
    )
    conn.execute(
        f"""INSERT INTO temp.bp_fact (id, key, thread)
            SELECT kf.id, sb_fact_key(kf.fact), {_THREAD_SQL}
            FROM key_facts kf
            LEFT JOIN temp.bp_email_thread t ON t.id = kf.email_id
            LEFT JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id"""
    )
    conn.execute(
        """CREATE TEMP TABLE bp_keys AS
           SELECT key, COUNT(DISTINCT thread) AS threads, MIN(id) AS first_id
           FROM temp.bp_fact WHERE key IS NOT NULL
           GROUP BY key HAVING COUNT(DISTINCT thread) >= ?""",
        (min_threads,),
    )
    conn.execute("CREATE INDEX temp.bp_keys_key ON bp_keys(key)")
    marked_sql = "SELECT f.id FROM temp.bp_fact f JOIN temp.bp_keys k ON k.key = f.key"
    stats = {
        "facts": conn.execute("SELECT COUNT(*) FROM temp.bp_fact").fetchone()[0],
        "keys": conn.execute("SELECT COUNT(*) FROM temp.bp_keys").fetchone()[0],
        "rows": conn.execute(f"SELECT COUNT(*) FROM ({marked_sql})").fetchone()[0],
        "already_marked": conn.execute(
            f"SELECT COUNT(*) FROM key_facts WHERE boilerplate = 1 AND id IN ({marked_sql})"
        ).fetchone()[0],
        "min_threads": min_threads,
        "applied": False,
    }
    stats["commonest"] = [
        (threads, text)
        for threads, text in conn.execute(
            "SELECT k.threads, kf.fact FROM temp.bp_keys k JOIN key_facts kf ON kf.id = k.first_id"
            " ORDER BY k.threads DESC, k.key LIMIT ?",
            (max(0, show),),
        )
    ]
    if apply:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO boilerplate_fact_keys (key, threads, marked_at)
                   SELECT key, threads, ? FROM temp.bp_keys WHERE true
                   ON CONFLICT(key) DO UPDATE SET threads = excluded.threads""",
                (_now(),),
            )
            cur = conn.execute(
                f"UPDATE key_facts SET boilerplate = 1 WHERE boilerplate = 0 AND id IN ({marked_sql})"
            )
            stats["newly_marked"] = cur.rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.rollback()
            raise
        stats["applied"] = True
    conn.executescript(
        "DROP TABLE temp.bp_email_thread; DROP TABLE temp.bp_fact; DROP TABLE temp.bp_keys;"
    )
    return stats


# --- items from news and automation ---------------------------------------------------------

ITEM_TABLES = ("decisions", "action_items", "commitments")


def _itemless_ids_sql() -> str:
    listed = ", ".join(f"'{name}'" for name in ITEMLESS_CLASSES)
    return f"SELECT id FROM emails WHERE source_class IN ({listed})"


def forget_itemless(conn: sqlite3.Connection, *, apply: bool = False) -> dict:
    """Count, and with `apply` delete in one transaction, the decisions, action items and
    commitments of news and automation emails. Returns {table: {class: rows}}, and
    'applied'."""
    _require_column(conn)
    ids = _itemless_ids_sql()
    stats: dict = {"applied": False}
    for table in ITEM_TABLES:
        stats[table] = dict(
            conn.execute(
                f"SELECT e.source_class, COUNT(*) FROM {table} i JOIN emails e ON e.id = i.email_id"
                f" WHERE i.email_id IN ({ids}) GROUP BY e.source_class ORDER BY 1"
            ).fetchall()
        )
    if apply:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ITEM_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE email_id IN ({ids})")
            conn.execute("COMMIT")
        except BaseException:
            conn.rollback()
            raise
        stats["applied"] = True
    return stats


# --- re-extracting the bodies of emails with stacked attachment facts -----------------------

MARKER = "stacked_reextract:"
MAX_ATTEMPTS = 3

# Emails holding both key facts no attachment wrote and key facts an attachment wrote.
STACKED_SQL = """SELECT email_id FROM key_facts WHERE email_id IS NOT NULL
    GROUP BY email_id
    HAVING SUM(attachment_id IS NULL) > 0 AND SUM(attachment_id IS NOT NULL) > 0"""


def _marks(conn: sqlite3.Connection) -> dict[int, dict]:
    out = {}
    for key, value in conn.execute(
        "SELECT key, value FROM sync_metadata WHERE key LIKE ?", (MARKER + "%",)
    ):
        try:
            out[int(key[len(MARKER) :])] = json.loads(value)
        except (ValueError, TypeError):
            continue
    return out


def stacked_emails(conn: sqlite3.Connection, limit: int = 0) -> list[int]:
    """The emails still to take, newest first: those STACKED_SQL selects, less those done and
    those that failed MAX_ATTEMPTS times."""
    marks = _marks(conn)
    rows = conn.execute(
        f"SELECT e.id FROM emails e WHERE e.id IN ({STACKED_SQL})"
        " ORDER BY e.date_received DESC, e.id DESC"
    ).fetchall()
    out = []
    for (email_id,) in rows:
        mark = marks.get(email_id) or {}
        if mark.get("state") == "done" or int(mark.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        out.append(email_id)
        if limit and len(out) >= limit:
            break
    return out


def _request_chars(email: dict) -> tuple[int, int]:
    """(system prompt characters, the rest) of the call extract_one would send for `email`."""
    from src.extract.call_shape import uses_schema_v2
    from src.extract.prompt import build_email_request_v2, build_extraction_prompt

    if uses_schema_v2():
        request = build_email_request_v2(email)
        system = sum(len(block["text"]) for block in request.blocks)
        return system, request.chars - system
    return 0, len(build_extraction_prompt(email))


def estimate_stacked(conn: sqlite3.Connection, email_ids: list[int]) -> dict:
    """The calls, tokens and cost of extracting these emails again, online. Input at the
    corpus's characters per token; output at OUTPUT_TOKENS_PER_CALL a call, an upper bound.
    `cost_usd` counts each system prompt in full; `cost_usd_cached` the first as a one-hour
    cache write and the rest as cache reads, as the calls are sent one after another."""
    from src.llm_cost import (
        ESTIMATE_MODEL,
        OUTPUT_TOKENS_PER_CALL,
        configured_chars_per_token,
        cost_usd,
        rates_for,
    )
    from src.store.loader import stored_email

    per_token = configured_chars_per_token()
    rates = rates_for(ESTIMATE_MODEL)
    assert rates is not None
    system_tokens = user_tokens = 0
    for email_id in email_ids:
        system, rest = _request_chars(stored_email(conn, email_id))
        system_tokens = max(system_tokens, math.ceil(system / per_token))
        user_tokens += math.ceil(rest / per_token)
    calls = len(email_ids)
    output = calls * OUTPUT_TOKENS_PER_CALL
    full = cost_usd(rates, input_tokens=user_tokens + calls * system_tokens, output_tokens=output)
    cached = cost_usd(
        rates,
        input_tokens=user_tokens,
        output_tokens=output,
        cache_write_1h_tokens=system_tokens if calls else 0,
        cache_read_tokens=max(0, calls - 1) * system_tokens,
    )
    return {
        "emails": calls,
        "calls": calls,
        "input_tokens": user_tokens + calls * system_tokens,
        "system_prompt_tokens": system_tokens,
        "output_tokens": output,
        "cost_usd": full,
        "cost_usd_cached": cached,
        "chars_per_token": per_token,
    }


def _record(conn: sqlite3.Connection, email_id: int, mark: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        (f"{MARKER}{email_id}", json.dumps(mark)),
    )


def reextract_stacked(
    conn: sqlite3.Connection,
    *,
    budget,
    extracted_dir: Path | None = None,
    limit: int = 0,
) -> dict:
    """Extract the bodies of the stacked emails again and give each the new items, newest
    first, until the budget (src/llm_cost.py TokenBudget) refuses a call, `limit` emails are
    done, or the service fails. One commit per email. The new extraction is written to the
    email's extraction file under `extracted_dir` when one is given, as stub_retry does."""
    from src.export.state import write_json_atomic
    from src.extract import claude_extract
    from src.extract.call_shape import call_shape
    from src.extract.extraction_files import extraction_path
    from src.extract.policy_bridge import is_transient
    from src.llm_cost import OverBudget
    from src.redact import redact_payload
    from src.store.loader import replace_items, stored_email

    shape = call_shape()
    marks = _marks(conn)
    stats: dict = {"selected": 0, "done": 0, "failed": 0, "facts_before": 0, "facts_after": 0}
    stats["stopped"] = stats["stop_kind"] = None
    for email_id in stacked_emails(conn, limit):
        stats["selected"] += 1
        email = stored_email(conn, email_id)
        try:
            extraction = claude_extract.extract_one(email, budget=budget)
        except OverBudget as e:
            stats["stopped"], stats["stop_kind"] = f"token budget: {e}", "budget"
            break
        except Exception as e:
            if is_transient(e):
                stats["stopped"] = f"{type(e).__name__}: the service failed; run again later"
                stats["stop_kind"] = "service"
                break
            attempts = int((marks.get(email_id) or {}).get("attempts") or 0) + 1
            _record(conn, email_id, {"state": "failed", "attempts": attempts, "at": _now()})
            conn.commit()
            stats["failed"] += 1
            continue
        if extraction is None:
            stats["failed"] += 1
            continue
        extraction = redact_payload(extraction)
        if extracted_dir is not None:
            write_json_atomic(extraction_path(extracted_dir, email["message_id"]), extraction)
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            counts = replace_items(conn, email_id, email, extraction)
            _record(
                conn,
                email_id,
                {"state": "done", "at": _now(), "shape": shape, **counts},
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.rollback()
            raise
        stats["done"] += 1
        stats["facts_before"] += counts["facts_before"]
        stats["facts_after"] += counts["facts_after"]
    stats["left"] = len(stacked_emails(conn))
    return stats


# --- the commands ---------------------------------------------------------------------------


def _connect(args) -> sqlite3.Connection:
    from src.store.schema import get_connection, run_migrations

    conn = get_connection(str(args.db))
    run_migrations(conn)
    return conn


def cmd_forget_itemless(args) -> int:
    conn = _connect(args)
    stats = forget_itemless(conn, apply=args.apply)
    conn.close()
    verb = "deleted" if stats["applied"] else "to delete"
    for table in ITEM_TABLES:
        by_class = stats[table]
        detail = ", ".join(f"{name} {count:,}" for name, count in by_class.items()) or "none"
        print(f"{table}: {sum(by_class.values()):,} {verb} ({detail})")
    if not stats["applied"]:
        print("dry run: nothing deleted; --apply deletes them")
    return 0


def cmd_mark_boilerplate(args) -> int:
    conn = _connect(args)
    stats = mark_boilerplate(conn, args.min_threads, apply=args.apply, show=args.show)
    conn.close()
    print(
        f"{stats['keys']:,} fact texts recur in {stats['min_threads']}+ threads:"
        f" {stats['rows']:,} of {stats['facts']:,} key facts,"
        f" {stats['already_marked']:,} of them already marked"
    )
    for threads, text in stats["commonest"]:
        print(f"  {threads:>6,} threads  {' '.join(str(text).split())[:90]}")
    if stats["applied"]:
        print(f"marked {stats.get('newly_marked', 0):,} rows as boilerplate")
    else:
        print("dry run: nothing written; --apply marks them")
    return 0


def cmd_reextract_stacked(args) -> int:
    from src.config import DATA_ROOT, EXTRACT_ENGINE
    from src.extract.call_shape import call_shape
    from src.llm_cost import TokenBudget

    if EXTRACT_ENGINE != "claude":
        print("reextract-stacked runs on the claude engine only", file=sys.stderr)
        return 2
    conn = _connect(args)
    shape = call_shape()
    if not args.apply:
        ids = stacked_emails(conn, args.limit)
        est = estimate_stacked(conn, ids)
        conn.close()
        print(
            f"dry run, call shape {shape}: {est['emails']:,} emails to extract again,"
            f" {est['calls']:,} calls, about {est['input_tokens']:,} input tokens"
            f" (a system prompt of {est['system_prompt_tokens']:,}) and at most"
            f" {est['output_tokens']:,} output tokens: ${est['cost_usd']:.2f} with every"
            f" system prompt in full, ${est['cost_usd_cached']:.2f} with it cached."
            " --apply --token-budget N runs it."
        )
        return 0
    if not args.token_budget or args.token_budget <= 0:
        conn.close()
        print("reextract-stacked --apply needs --token-budget N (tokens, N > 0)", file=sys.stderr)
        return 2
    budget = TokenBudget(args.token_budget)
    stats = reextract_stacked(
        conn, budget=budget, extracted_dir=Path(DATA_ROOT) / "extracted", limit=args.limit
    )
    conn.close()
    print(
        f"call shape {shape}: {stats['done']:,} emails given new items, {stats['failed']:,}"
        f" failed, {stats['left']:,} left; untagged key facts {stats['facts_before']:,} ->"
        f" {stats['facts_after']:,}; {budget.spent:,} tokens in {budget.calls:,} calls"
    )
    if stats["stopped"]:
        print(f"stopped: {stats['stopped']}")
    # 75 (EX_TEMPFAIL) when the service stopped the run, as retry-stubs does; a budget stop
    # is the run ending as asked.
    return 75 if stats["stop_kind"] == "service" else 0


def add_maint_parsers(maint_subparsers) -> None:
    """The three commands, under `brain maint`."""
    forget = maint_subparsers.add_parser(
        "forget-itemless",
        help="Delete the decisions, actions and commitments of news and automation mail",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    forget.add_argument("--apply", action="store_true", help="Delete (default: count only)")
    forget.set_defaults(func=cmd_forget_itemless)

    mark = maint_subparsers.add_parser(
        "mark-boilerplate",
        help="Mark key-fact text that recurs across threads as boilerplate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mark.add_argument("--apply", action="store_true", help="Mark (default: count only)")
    mark.add_argument(
        "--min-threads",
        type=int,
        default=DEFAULT_MIN_THREADS,
        help=f"Distinct threads a text must recur in (default {DEFAULT_MIN_THREADS})",
    )
    mark.add_argument("--show", type=int, default=10, help="Commonest texts to print")
    mark.set_defaults(func=cmd_mark_boilerplate)

    stacked = maint_subparsers.add_parser(
        "reextract-stacked",
        help="Extract again the bodies of emails with stacked attachment facts",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stacked.add_argument(
        "--apply", action="store_true", help="Call the model and replace (default: estimate)"
    )
    stacked.add_argument(
        "--token-budget", type=int, default=0, help="Tokens a run may spend (required with --apply)"
    )
    stacked.add_argument("--limit", type=int, default=0, help="Emails a run takes (0: all)")
    stacked.set_defaults(func=cmd_reextract_stacked)

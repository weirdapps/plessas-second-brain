"""`brain topic-pages`: write the page of each live topic whose evidence is new.

The topics, their evidence and the stored pages are src/store/topic_pages.py's. This is
the job: one model call a page through claude_extract.complete(), as plain JSON in the
reply (the deployment's Vertex project refuses structured outputs), then the check that keeps
only the lines whose refs were in the evidence, then the row. A page is rebuilt only when
its evidence holds an item it was not written from, or the prompt changed.
"""

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.store.normalizer import normalize_topic
from src.store.topic_pages import (
    EVIDENCE_DAYS,
    Evidence,
    LiveTopic,
    gather_evidence,
    held_page,
    named_topics,
    retire_pages,
    select_live_topics,
)

# -------------------------------------------------------------------- prompt

PROMPT_VERSION = "topic-page-1"
_SECTIONS = {
    "status": ("Current status", 6),
    "decisions": ("Decisions", 12),
    "numbers": ("Settled numbers", 12),
    "open_questions": ("Open questions", 8),
    "deliverables": ("Deliverables produced", 8),
    "superseded": ("Superseded", 8),
}
_EVIDENCE_TITLES = {
    "decision": "DECISIONS (primary)",
    "fact": "KEY FACTS (primary)",
    "action": "OPEN ACTIONS (primary)",
    "email": "EMAILS (primary)",
    "attachment": "ATTACHMENTS (primary)",
    "figure": "FIGURES from recurring reports (primary)",
    "finding": "FINDINGS filed by earlier research sessions (derived: below every primary row)",
    "session": "THE OWNER'S WORK SESSIONS (derived: for open questions and deliverables only)",
    "deliverable": "NOTES SESSIONS WROTE (derived: list as deliverables, never as facts)",
}

SYSTEM = """\
You write one topic page for an executive's personal knowledge base: where a live topic \
stands, for the next session to start from. You are given the topic and numbered \
evidence rows, each with a cite key such as email:123 or attachment:45.

Rules:
- Every line rests on the evidence: give the cite keys of the rows it comes from in \
"refs". A line you cannot cite, leave out. Never invent a key, a number, a date or a name.
- Primary rows (emails, decisions, facts, actions, attachments, figures) outrank derived \
rows. A finding (finding:N) may support a line only where no primary row says otherwise, \
and never removes or contradicts a primary row; one marked SUPERSEDED is not a settled \
number, and one filed by an agent session ranks below one filed by the owner's. Work \
sessions and notes are for open questions and deliverables only.
- Numbers: only values the evidence states, each with its period and its source (the \
report or sender), as "metric: value".
- When newer evidence changes an older value or decision, give the newer one under its \
section and the older one under "superseded", saying what replaced it, with both keys.
- Sections: "status" is where things stand now; "decisions" what was decided, dated; \
"open_questions" what is still undecided or owed, by whom and by when; "deliverables" \
what has already been produced (a deck, a report, a press release, a reconciliation, a \
note a session wrote), dated, never a task still to do.
- Dates as YYYY-MM-DD. Write in English, short lines, no repetition.

Reply with one JSON object and nothing else:
{"status": [{"text": "...", "refs": ["email:1"]}],
 "decisions": [{"date": "YYYY-MM-DD", "text": "...", "refs": ["..."]}],
 "numbers": [{"text": "metric: value", "period": "...", "source": "...", "refs": ["..."]}],
 "open_questions": [{"text": "...", "refs": ["..."]}],
 "deliverables": [{"date": "YYYY-MM-DD", "text": "...", "refs": ["..."]}],
 "superseded": [{"text": "older claim, replaced by ...", "refs": ["..."]}]}
At most 6 status lines, 12 decisions, 12 numbers, 8 open questions, 8 deliverables and \
8 superseded lines; an empty list where the evidence has nothing.\
"""


def prompt_hash() -> str:
    """Which prompt wrote a page: a change rebuilds every page."""
    return hashlib.sha256(f"{PROMPT_VERSION}\n{SYSTEM}".encode()).hexdigest()[:12]


def build_prompt(live: LiveTopic, evidence: Evidence, *, today: str) -> str:
    """The user turn: the topic, then the evidence fenced as third-party text."""
    from src.extract.untrusted import fence

    lines = []
    for section, title in _EVIDENCE_TITLES.items():
        rows = evidence.rows.get(section) or []
        if not rows:
            continue
        lines.append(title)
        for row in rows:
            lines.append(f"[{row['key']}] {row['date'] or 'undated'}: {row['text']}")
        lines.append("")
    covered = ", ".join(name for _, name in live.members) or live.name
    return f"Topic: {live.name} (tags covered: {covered}). Today: {today}.\n\n" + fence(
        "\n".join(lines).strip()
    )


# ------------------------------------------------------------------ the call

MAX_TOKENS = 6_000
EFFORT = "medium"


@dataclass
class Reply:
    text: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    stop_reason: str | None


def _cost(response) -> tuple[int, int, float | None]:
    from src.llm_cost import cost_usd, rates_for

    usage = getattr(response, "usage", None)

    def tokens(name: str) -> int:
        value = getattr(usage, name, None)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    rates = rates_for(getattr(response, "model", None))
    cost = None
    if rates is not None:
        cost = cost_usd(
            rates,
            input_tokens=tokens("input_tokens"),
            output_tokens=tokens("output_tokens"),
            cache_read_tokens=tokens("cache_read_input_tokens"),
            cache_write_tokens=tokens("cache_creation_input_tokens"),
        )
    return tokens("input_tokens"), tokens("output_tokens"), cost


def ask(system: str, user: str, *, max_tokens: int = MAX_TOKENS) -> Reply:
    """One plain-text call through claude_extract.complete(): no structured output."""
    from src.extract.claude_extract import _response_text, complete

    response = complete(
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": EFFORT},
    )
    input_tokens, output_tokens, cost = _cost(response)
    stop = getattr(response, "stop_reason", None)
    text = "" if stop == "refusal" else _response_text(response)
    return Reply(text, getattr(response, "model", None), input_tokens, output_tokens, cost, stop)


# ---------------------------------------------------------------- the check

_REF_SPLIT = re.compile(r"[\s,;\[\]]+")


def _refs(item: dict) -> list[str]:
    raw = item.get("refs")
    if isinstance(raw, str):
        raw = _REF_SPLIT.split(raw)
    if not isinstance(raw, list):
        return []
    out = []
    for ref in raw:
        if isinstance(ref, str) and ref.strip():
            kind, _, rest = ref.strip().partition(":")
            out.append(f"{kind.strip()}:{rest.strip()}")
    return list(dict.fromkeys(out))


def check_page(reply: dict, keys: set[str]) -> tuple[dict[str, list[dict]], int]:
    """The lines of `reply` whose refs are all in `keys`, by section, and how many were
    dropped: a line with no ref, a ref the evidence did not hold, or no text."""
    kept: dict[str, list[dict]] = {}
    dropped = 0
    for section, (_, most) in _SECTIONS.items():
        items = reply.get(section) or []
        if not isinstance(items, list):
            dropped += 1
            continue
        for item in items:
            if not isinstance(item, dict):
                dropped += 1
                continue
            text = " ".join(str(item.get("text") or "").split())
            refs = _refs(item)
            if not text or not refs or any(ref not in keys for ref in refs):
                dropped += 1
                continue
            line = {"text": text, "refs": refs}
            for extra in ("date", "period", "source"):
                if isinstance(item.get(extra), str) and item[extra].strip():
                    line[extra] = " ".join(item[extra].split())
            bucket = kept.setdefault(section, [])
            if len(bucket) < most:
                bucket.append(line)
            else:
                dropped += 1
    return kept, dropped


def render(live: LiveTopic, sections: dict[str, list[dict]], built_at: str, count: int) -> str:
    """The page as markdown: every line ends with the keys it rests on."""
    out = [
        f"# {live.name}",
        "",
        f"Built {built_at} from {count} store items. Every line cites the items it rests "
        "on; get_item(kind, ref) reads each. A line citing finding:N rests on a finding a "
        "session filed, not on a primary item.",
    ]
    for section, (title, _) in _SECTIONS.items():
        lines = sections.get(section) or []
        if not lines:
            continue
        out += ["", f"## {title}"]
        for line in lines:
            lead = f"{line['date']}: " if line.get("date") else ""
            tail = "; ".join(line[k] for k in ("period", "source") if line.get(k))
            tail = f" ({tail})" if tail else ""
            out.append(f"- {lead}{line['text']}{tail} [{'; '.join(line['refs'])}]")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------- building


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_dirty(held: dict | None, evidence: Evidence) -> str | None:
    """Why the page must be written again, or None when it is current: no page yet, the
    prompt changed, or the evidence holds an item the page was not written from."""
    if held is None:
        return "no page yet"
    if held["prompt_hash"] != prompt_hash():
        return "prompt changed"
    new = evidence.keys - set(json.loads(held["evidence_keys"] or "[]"))
    if new:
        return f"{len(new)} new item{'s' if len(new) != 1 else ''}"
    return None


def store_page(
    conn: sqlite3.Connection,
    live: LiveTopic,
    evidence: Evidence,
    sections: dict[str, list[dict]],
    dropped: int,
    reply: Reply,
    built_at: str,
) -> None:
    used = list(
        dict.fromkeys(ref for lines in sections.values() for x in lines for ref in x["refs"])
    )
    conn.execute(
        """INSERT INTO topic_pages (topic, display_name, members, selected_by, body, citations,
               evidence_keys, built_at, newest_evidence_at, prompt_hash, model, input_tokens,
               output_tokens, cost_usd, lines_kept, lines_dropped)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(topic) DO UPDATE SET display_name = excluded.display_name,
               members = excluded.members, selected_by = excluded.selected_by,
               body = excluded.body, citations = excluded.citations,
               evidence_keys = excluded.evidence_keys, built_at = excluded.built_at,
               newest_evidence_at = excluded.newest_evidence_at,
               prompt_hash = excluded.prompt_hash, model = excluded.model,
               input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens,
               cost_usd = excluded.cost_usd, lines_kept = excluded.lines_kept,
               lines_dropped = excluded.lines_dropped""",
        (
            live.key,
            live.name,
            json.dumps([{"id": i, "name": n} for i, n in live.members], ensure_ascii=False),
            live.selected_by,
            render(live, sections, built_at, evidence.count),
            json.dumps([evidence.label(ref) for ref in used], ensure_ascii=False),
            json.dumps(sorted(evidence.keys)),
            built_at,
            evidence.newest,
            prompt_hash(),
            reply.model,
            reply.input_tokens,
            reply.output_tokens,
            reply.cost_usd,
            sum(len(lines) for lines in sections.values()),
            dropped,
        ),
    )
    conn.commit()


def estimate_tokens(prompt: str) -> tuple[int, int, float]:
    """Input and output tokens, and dollars, a page's call is expected to take."""
    from src.llm_cost import (
        ESTIMATE_MODEL,
        RATES,
        configured_chars_per_token,
        cost_usd,
    )

    input_tokens = round(len(SYSTEM + prompt) / configured_chars_per_token())
    output_tokens = 2_500  # the JSON page plus medium-effort thinking
    cost = cost_usd(RATES[ESTIMATE_MODEL], input_tokens=input_tokens, output_tokens=output_tokens)
    return input_tokens, output_tokens, cost


def build_pages(
    conn: sqlite3.Connection,
    topics: list[LiveTopic],
    *,
    force: bool = False,
    estimate: bool = False,
    deadline_s: float | None = None,
    log: Callable[[str], None] = print,
    now: datetime | None = None,
) -> dict:
    """Write the page of every topic in `topics` whose evidence is new (all with `force`).

    With `estimate`, write nothing and call no model: count the calls and price them.
    `deadline_s` stops before a page once that many seconds have passed. Returns counts:
    built, clean, empty, failed, estimated, lines_dropped, cost_usd, tokens.
    """
    started = time.monotonic()
    now = now or datetime.now(UTC)
    stats: dict[str, Any] = {"selected": len(topics), "built": 0, "clean": 0, "empty": 0}
    stats["failed"] = 0
    stats.update({"to_build": 0, "lines_dropped": 0, "cost_usd": 0.0})
    stats.update({"input_tokens": 0, "output_tokens": 0, "deadline": False})
    for n, live in enumerate(topics, start=1):
        label = f"page {n}/{len(topics)} ({live.selected_by})"
        evidence = gather_evidence(conn, live, now=now)
        if not evidence.count:
            stats["empty"] += 1
            log(f"  {label}: no evidence in {EVIDENCE_DAYS} days, skipped")
            continue
        why = "forced" if force else is_dirty(held_page(conn, live.key), evidence)
        if why is None:
            stats["clean"] += 1
            continue
        prompt = build_prompt(live, evidence, today=now.strftime("%Y-%m-%d"))
        stats["to_build"] += 1
        if estimate:
            tokens_in, tokens_out, cost = estimate_tokens(prompt)
            stats["input_tokens"] += tokens_in
            stats["output_tokens"] += tokens_out
            stats["cost_usd"] += cost
            log(f"  {label}: {why}; {evidence.count} items, ~{tokens_in:,} tokens in")
            continue
        if deadline_s is not None and time.monotonic() - started > deadline_s:
            stats["deadline"] = True
            log(f"  deadline of {deadline_s:.0f} s reached; the rest wait for the next run")
            break
        try:
            reply = ask(SYSTEM, prompt)
            if reply.stop_reason in ("refusal", "max_tokens"):
                raise ValueError(f"the reply stopped: {reply.stop_reason}")
            from src.extract.parser import _json_object

            sections, dropped = check_page(_json_object(reply.text), evidence.keys)
        except Exception as e:
            stats["failed"] += 1
            log(f"  {label}: failed, the page held is kept ({type(e).__name__})")
            continue
        if not sections:
            stats["failed"] += 1
            stats["lines_dropped"] += dropped
            log(f"  {label}: no line survived the citation check; the page held is kept")
            continue
        store_page(conn, live, evidence, sections, dropped, reply, _now_utc())
        stats["built"] += 1
        stats["lines_dropped"] += dropped
        stats["input_tokens"] += reply.input_tokens
        stats["output_tokens"] += reply.output_tokens
        stats["cost_usd"] += reply.cost_usd or 0.0
        log(
            f"  {label}: built ({why}); {sum(len(v) for v in sections.values())} lines, "
            f"{dropped} dropped, ${reply.cost_usd or 0:.3f}"
        )
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats


# ---------------------------------------------------------------------- CLI


def cmd(args) -> int:
    """`brain topic-pages`: write the pages of the live topics whose evidence is new.

    `--build` writes every page again (`brain maint topic-pages`), `--topic` only the
    pages named (a name not picked is built as BRAIN_TOPIC_PAGES would build it), and
    `--estimate` prices what would be written and writes nothing, so it runs on a replica.
    Exits 1 when pages were due and every one of them failed.
    """
    import sys

    from src import config
    from src.store.schema import get_connection, run_migrations
    from src.store.sql_readonly import connect_read_only

    if not args.estimate and config.is_replica():
        print(config.replica_refusal("'topic-pages'"), file=sys.stderr)
        return 2
    if args.estimate:
        conn = connect_read_only(args.db, immutable=config.is_pulled_copy())
    else:
        conn = get_connection(str(args.db))
        run_migrations(conn)
    try:
        topics = select_live_topics(conn)
        if args.topic:
            wanted = [named_topics(name)[0] for name in args.topic if named_topics(name)]
            by_key = {t.key: t for t in topics}
            chosen = []
            for phrases in wanted:
                key = normalize_topic(phrases[0])
                if key in by_key:
                    chosen.append(by_key[key])
                else:
                    chosen += select_live_topics(conn, named=[phrases], cap=1)[:1]
            topics = chosen
        by_kind: dict[str, int] = {}
        for t in topics:
            by_kind[t.selected_by] = by_kind.get(t.selected_by, 0) + 1
        print(
            f"topic pages: {len(topics)} topics ("
            + ", ".join(f"{n} {kind}" for kind, n in sorted(by_kind.items()))
            + ")"
        )
        for t in topics:
            print(
                f"  {t.name}: {t.selected_by}, {t.sessions} sessions, {t.threads} threads, "
                f"{len(t.members)} tags"
            )
        stats = build_pages(
            conn,
            topics,
            force=args.build,
            estimate=args.estimate,
            deadline_s=args.deadline_s,
        )
        if not args.estimate and not args.topic:
            stats["retired"] = retire_pages(conn, [t.key for t in topics])
    finally:
        conn.close()
    print("topic pages: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return 1 if not args.estimate and stats["failed"] and not stats["built"] else 0

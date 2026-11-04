"""`brain pages`: the synthesis of the compiled pages (issue 169).

The pages, their skeletons, dirty flags, generations and overrides are src/store/pages.py's;
the sync compiles the skeletons. This is the job that writes the synthesis: one model call a
page through the topic pages' `ask` (claude_extract.complete(), plain JSON in the reply,
since the deployment's Vertex project refuses structured outputs), then the check that keeps
only the lines whose refs were in the evidence the model was given, at most SYNTHESIS_WORDS
words, each line dated by its refs.

Which pages, in order, within `--llm-calls` calls: pages never written, then pages marked
dirty (new items, an override, a forgotten item), then pages an older prompt wrote. `--full`
writes every eligible page again into a new generation, a budget a night, and swaps a kind
in once all its pages are written, so a reader never sees half a rebuild. `--estimate`
prices the calls and writes nothing.
"""

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from src.pipeline.topic_pages import Reply, ask  # the one LLM path the pages share
from src.store import pages

__all__ = ["Reply", "ask", "build", "cmd"]

PROMPT_VERSION = "page-1"
MAX_LINES = 8
MAX_TOKENS = 3_000
_EVIDENCE_ROWS = 15
_TEXT = 500

SYSTEM = """\
You write one compiled page for an executive's personal knowledge base: a short account of \
one {kind} the store holds, for a session to start from. You are given the page's facts \
and numbered evidence rows, each with a cite key such as email:123 or calendar:45, and \
its date.

Rules:
- At most 8 lines and 300 words in all. Lead with where things stand now, then what \
was decided, then what is still owed and by whom. Short lines, no repetition.
- Every line rests on the evidence: give the cite keys of the rows it comes from in \
"refs". A line you cannot cite, leave out. Never invent a key, a number, a date or a name.
- When newer evidence changes an older value or decision, give the newer one and say \
what it replaced.
- The owner's corrections, when given, are true: never contradict them. Do not cite them.
- Say nothing about what the evidence lacks: no line saying there is no decision or no \
owner. A routine fact (an organiser, a count, a date already in the facts) needs no line.
- Dates as YYYY-MM-DD. Write in English.

Reply with one JSON object and nothing else:
{{"lines": [{{"text": "...", "refs": ["email:1"]}}]}}\
"""


def system_for(kind: str) -> str:
    return SYSTEM.format(kind={"series": "meeting series", "thread": "mail thread"}.get(kind, kind))


def prompt_hash() -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\n{SYSTEM}".encode()).hexdigest()[:12]


# --- evidence ------------------------------------------------------------------------------


def _cut(text: str | None, chars: int = _TEXT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= chars else text[: chars - 1] + "…"


class Evidence:
    """What one synthesis may cite: cite key -> (date, text), in the order given."""

    def __init__(self, dropped: set[str]):
        self.rows: dict[str, tuple[str | None, str]] = {}
        self._dropped = dropped

    def add(self, key: str, date: str | None, text: str) -> None:
        if key and text and key not in self._dropped and key not in self.rows:
            self.rows[key] = ((date or "")[:10] or None, _cut(text))

    @property
    def keys(self) -> set[str]:
        return set(self.rows)


def _mail(conn: sqlite3.Connection, sql: str, args: tuple, evidence: Evidence) -> None:
    for email_id, date, subject, summary in conn.execute(sql, args):
        evidence.add(f"email:{email_id}", date, f"«{_cut(subject, 140)}»: {_cut(summary)}")


def gather(conn: sqlite3.Connection, kind: str, key: str, page: dict) -> Evidence:
    """The skeleton's cited lines, then the entity's own items, newest first; never a
    derived row (a finding, a session, another page), and never a ref an override drops."""
    evidence = Evidence(pages.dropped_refs(conn, kind, key))
    for line in page["lines"]:
        for ref in line["refs"]:
            evidence.add(ref, line.get("date"), line["text"])
    cols = "e.id, e.date_received, e.subject, e.summary"
    if kind == "person":
        _mail(
            conn,
            f"""SELECT {cols} FROM emails e WHERE e.source_class = 'mail' AND e.id IN
                (SELECT email_id FROM email_people WHERE person_id = ?)
                ORDER BY e.date_received DESC LIMIT ?""",
            (int(key), _EVIDENCE_ROWS),
            evidence,
        )
    elif kind == "thread":
        _mail(
            conn,
            f"""SELECT {cols} FROM emails e WHERE e.conversation_id = ?
                ORDER BY e.date_received DESC LIMIT ?""",
            (key, _EVIDENCE_ROWS),
            evidence,
        )
    elif kind == "series":
        for event_id, start, subject, summary in conn.execute(
            """SELECT id, start_at, subject, body_summary FROM calendar_events
               WHERE recurrence_master_id = ? AND is_cancelled = 0
               ORDER BY start_at DESC LIMIT 8""",
            (key,),
        ):
            evidence.add(f"calendar:{event_id}", start, f"«{_cut(subject, 140)}»: {_cut(summary)}")
    elif kind == "topic":
        _topic_evidence(conn, key, page, evidence)
    return evidence


def _topic_evidence(conn: sqlite3.Connection, key: str, page: dict, evidence: Evidence) -> None:
    """The topic pages' gatherer (src/store/topic_pages.py) over the canonical's members,
    primary sections only: the derived ones (findings, sessions, notes) stay out."""
    from src.store.topic_pages import LiveTopic, gather_evidence

    members = pages._topic_members(conn, key)
    names = dict(
        conn.execute(
            "SELECT id, name FROM topics WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(members),),
        ).fetchall()
    )
    live = LiveTopic(page["name"], [(t, names[t]) for t in members if t in names], "named")
    found = gather_evidence(conn, live)
    for section in ("decision", "fact", "action", "email", "attachment", "figure"):
        for row in found.rows.get(section) or []:
            evidence.add(row["key"], row["date"], row["text"])


def build_prompt(kind: str, page: dict, evidence: Evidence) -> str:
    from src.extract.untrusted import fence

    facts = {k: v for k, v in page["facts"].items() if v not in (None, [], "")}
    rows = [f"[{k}] {d or 'undated'}: {t}" for k, (d, t) in evidence.rows.items()]
    head = f"The {kind}: {page['name']}. Facts: {json.dumps(facts, ensure_ascii=False)}."
    if page["corrections"]:
        head += "\nThe owner's corrections (true; do not cite): " + "; ".join(
            c["text"] for c in page["corrections"]
        )
    return head + "\n\n" + fence("\n".join(rows))


# --- the check -----------------------------------------------------------------------------

_REF_SPLIT = re.compile(r"[\s,;\[\]]+")


def _refs(item: dict) -> list[str]:
    raw = item.get("refs")
    if isinstance(raw, str):
        raw = _REF_SPLIT.split(raw)
    if not isinstance(raw, list):
        return []
    out = []
    for ref in raw:
        if isinstance(ref, str) and ":" in ref:
            kind, _, rest = ref.strip().partition(":")
            out.append(f"{kind.strip()}:{rest.strip()}")
    return list(dict.fromkeys(out))


def check(reply: dict, evidence: Evidence) -> tuple[list[dict], int]:
    """The lines whose every ref the evidence held, each with its refs' dates, at most
    MAX_LINES and SYNTHESIS_WORDS words; and how many were dropped."""
    kept: list[dict] = []
    dropped = 0
    words = 0
    items = reply.get("lines") if isinstance(reply, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        text = " ".join(str(item.get("text") or "").split())
        refs = _refs(item)
        n = len(text.split())
        if (
            not text
            or not refs
            or any(r not in evidence.rows for r in refs)
            or len(kept) >= MAX_LINES
            or words + n > pages.SYNTHESIS_WORDS
        ):
            dropped += 1
            continue
        words += n
        kept.append({"text": text, "refs": refs, "dates": {r: evidence.rows[r][0] for r in refs}})
    return kept, dropped


# --- the job -------------------------------------------------------------------------------


def _due(conn: sqlite3.Connection, kinds: list[str], generation_of: dict[str, int], full: bool):
    """(kind, key) of the pages to write, in order: never written, dirty, older prompt."""
    out = []
    for kind in kinds:
        gen = generation_of[kind]
        if full:
            sql = (
                "SELECT entity_key FROM pages WHERE kind = ? AND generation = ?"
                " AND synthesis IS NULL ORDER BY entity_key"
            )
            out += [(0, "", kind, k) for (k,) in conn.execute(sql, (kind, gen))]
            continue
        sql = """SELECT p.entity_key, p.synthesis IS NULL, d.marked_at, p.prompt_hash
                 FROM pages p LEFT JOIN page_dirty d
                   ON d.kind = p.kind AND d.entity_key = p.entity_key
                 WHERE p.kind = ? AND p.generation = ?
                   AND (p.synthesis IS NULL OR d.kind IS NOT NULL OR p.prompt_hash <> ?)"""
        for key, unwritten, marked, _ in conn.execute(sql, (kind, gen, prompt_hash())):
            rank = 0 if unwritten else (1 if marked else 2)
            out.append((rank, marked or "", kind, key))
    out.sort(key=lambda r: (r[0], r[1], pages.KINDS.index(r[2]), r[3]))
    return [(kind, key) for _, _, kind, key in out]


def estimate_tokens(system: str, prompt: str) -> tuple[int, int, float]:
    from src.llm_cost import ESTIMATE_MODEL, RATES, configured_chars_per_token, cost_usd

    tokens_in = round(len(system + prompt) / configured_chars_per_token())
    tokens_out = 1_200
    return (
        tokens_in,
        tokens_out,
        cost_usd(RATES[ESTIMATE_MODEL], input_tokens=tokens_in, output_tokens=tokens_out),
    )


def build(
    conn: sqlite3.Connection,
    *,
    kinds: list[str] | None = None,
    llm_calls: int = 40,
    full: bool = False,
    estimate: bool = False,
    deadline_s: float | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Write the synthesis of the pages due, at most `llm_calls` calls. Returns counts."""
    from src.extract.parser import _json_object

    kinds = list(kinds or pages.KINDS)
    started = time.monotonic()
    stats: dict[str, Any] = {"to_build": 0, "built": 0, "failed": 0, "empty": 0}
    stats.update({"lines_dropped": 0, "calls": 0, "cost_usd": 0.0, "left": 0, "swapped": []})
    stats.update({"input_tokens": 0, "output_tokens": 0})
    generation_of = {}
    for kind in kinds:
        if full and not estimate:
            gen = pages.begin_build(conn, kind)
            keep = pages.eligible(conn, kind)
            conn.execute(
                "DELETE FROM pages WHERE kind = ? AND generation = ?"
                " AND entity_key NOT IN (SELECT value FROM json_each(?))",
                (kind, gen, json.dumps(keep)),
            )
            conn.commit()
            generation_of[kind] = gen
        else:
            generation_of[kind] = pages.live_generation(conn, kind)
    due = _due(conn, kinds, generation_of, full)
    stats["to_build"] = len(due)
    for kind, key in due:
        if stats["calls"] >= llm_calls or (
            deadline_s is not None and time.monotonic() - started > deadline_s
        ):
            break
        page = pages.get_page(conn, kind, key, generation=generation_of[kind])
        if page is None:  # hidden by an override
            stats["empty"] += 1
            continue
        evidence = gather(conn, kind, key, page)
        if not evidence.rows:
            stats["empty"] += 1
            continue
        system, prompt = system_for(kind), build_prompt(kind, page, evidence)
        stats["calls"] += 1
        if estimate:
            tokens_in, tokens_out, cost = estimate_tokens(system, prompt)
            stats["input_tokens"] += tokens_in
            stats["output_tokens"] += tokens_out
            stats["cost_usd"] += cost
            continue
        try:
            reply = ask(system, prompt, max_tokens=MAX_TOKENS)
            if reply.stop_reason in ("refusal", "max_tokens"):
                raise ValueError(f"the reply stopped: {reply.stop_reason}")
            lines, dropped = check(_json_object(reply.text), evidence)
        except Exception as e:
            stats["failed"] += 1
            log(f"  {kind} {key}: failed, the page keeps what it had ({type(e).__name__})")
            continue
        stats["lines_dropped"] += dropped
        stats["input_tokens"] += reply.input_tokens
        stats["output_tokens"] += reply.output_tokens
        stats["cost_usd"] += reply.cost_usd or 0.0
        if not lines:
            stats["failed"] += 1
            log(f"  {kind} {key}: no line survived the citation check; kept as it was")
            continue
        pages.write_synthesis(
            conn,
            kind,
            key,
            lines,
            generation=generation_of[kind],
            meta={
                "evidence_keys": evidence.keys,
                "prompt_hash": prompt_hash(),
                "model": reply.model,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "cost_usd": reply.cost_usd,
                "lines_dropped": dropped,
            },
        )
        stats["built"] += 1
        log(f"  {kind} {page['name'][:60]}: {len(lines)} lines, {dropped} dropped")
    if full and not estimate:
        for kind in kinds:
            left = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE kind = ? AND generation = ?"
                " AND synthesis IS NULL",
                (kind, generation_of[kind]),
            ).fetchone()[0]
            if not left:
                pages.swap(conn, kind)
                stats["swapped"].append(kind)
    stats["left"] = max(0, stats["to_build"] - stats["built"] - stats["empty"])
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats


# --- CLI -----------------------------------------------------------------------------------


def cmd(args) -> int:
    """`brain pages`: write the synthesis of the dirty pages (nightly), or `--full` all of
    them into a new generation; `--refresh` compiles the skeletons as the sync does,
    `--status` counts, `--estimate` prices; `--override KIND KEY ACTION`, `--overrides` and
    `--remove-override ID` keep the corrections. Exits 2 on a replica for anything that
    writes, 1 when pages were due and every call failed."""
    import sys

    from src import config
    from src.store.schema import ensure_schema, get_connection
    from src.store.sql_readonly import connect_read_only

    reads = args.estimate or args.status or args.overrides
    if not reads and config.is_replica():
        print(config.replica_refusal("'pages'"), file=sys.stderr)
        return 2
    if reads:
        conn = connect_read_only(args.db, immutable=config.is_pulled_copy())
    else:
        conn = get_connection(str(args.db))
        ensure_schema(conn)
    try:
        if not pages.enabled(conn):
            print("pages: no page layer on this store (the gated step page_layer)")
            return 0
        if args.status:
            print(json.dumps(pages.counts(conn), indent=1))
            return 0
        if args.overrides:
            for row in pages.overrides(conn):
                print(json.dumps(row, ensure_ascii=False))
            return 0
        if args.remove_override is not None:
            ok = pages.remove_override(conn, args.remove_override)
            print("pages: override removed" if ok else "pages: no such override")
            return 0 if ok else 1
        if args.override:
            kind, key, action = args.override
            new = pages.add_override(conn, kind, key, action, ref=args.ref, text=args.text)
            print(f"pages: override {new} added; it applies when the page is next read")
            return 0
        if args.refresh:
            stats = pages.refresh(conn)
            conn.commit()
            print("pages: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
            return 0
        stats = build(
            conn,
            kinds=args.kind,
            llm_calls=args.llm_calls,
            full=args.full,
            estimate=args.estimate,
            deadline_s=args.deadline_s,
        )
    finally:
        conn.close()
    print("pages: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return 1 if not args.estimate and stats["failed"] and not stats["built"] else 0

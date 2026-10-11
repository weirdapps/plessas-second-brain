#!/usr/bin/env python3
"""Retrieval evals: a known-item guard for keyword search, and a qa set of real questions.

Known-item (`build`, `run`): can an email be found by two words of its own subject?
A regression guard, run before and after changing how text is tokenised (stemming,
prefix matching): a change that loses emails shows up as a drop. It cannot show a
gain. The queries are the subject's own words, which the subject index matches
exactly already, so a looser match can only add competition.

`build` samples emails with at least two search words in their subject, leaving news
out, and keeps the two longest as the query; the email is the answer. Only subjects
no other thread shares both words of are kept: in the audit of 2026-10-11, items whose
two words sat in one thread's subject came first 65 times in 66 and items whose words
sat in more than ten came first 4 times in 61, so a shared query measured ambiguity,
not retrieval. The set holds subjects, so it is written outside the repository
(default ~/.second-brain/retrieval-eval.json). `run` searches each query the way
search_emails does and reports how often the answer comes back first, in the top 5
and in the top 10, and the mean reciprocal rank, twice: for the email itself, and for
its thread (the email or any other in its conversation). Replies share the subject,
so which of them ranks first is a tie-break, and keyword search returns one email per
thread for a subject match; the thread figures are the ones to compare.

QA (`--build-qa`, `--run-qa`): questions the owner put to the brain, each labelled with
the items that answer it. `--build-qa` drafts candidates from the owner's own logged
sessions: a user turn answered with a call to a second-brain tool. It writes the
question and the tools called, with every gold list empty for the owner to fill; the
sessions of the brain's own repository are left out unless --all-workspaces. `--run-qa`
asks each labelled question in four modes, the way the tools do (keyword and semantic
as search_emails, hybrid as recall's fused email bucket, recall as its whole bundle),
and grades recall@5, recall@10, MRR and nDCG@10 per mode on the gold kinds each mode
can return. It records the semantic status of every semantic and recall call, so an
embedding outage shows. Its only model calls are query embeddings.

Every run appends a dated record to evals/retrieval/results.jsonl, which
src/evals/summary.py reads for the health check. Everything opens the database
read-only, so it is safe on a replica, and nothing printed holds a question or a
subject: case ids, counts and rates.

    python scripts/retrieval_eval.py build [--size 20] [--seed 7]
    python scripts/retrieval_eval.py run
    python scripts/retrieval_eval.py --build-qa [--size 60] [--all-workspaces]
    python scripts/retrieval_eval.py --run-qa
"""

import argparse
import json
import math
import random
import re
import sqlite3
import sys
from collections import Counter
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DEFAULT_DB  # noqa: E402
from src.evals import (  # noqa: E402
    append_jsonl,
    connect_read_only,
    eval_home,
    file_sha,
    inside_repo,
    read_jsonl,
    stamp,
    write_jsonl,
    write_private,
)
from src.store.greek import search_fold, search_words  # noqa: E402

DEFAULT_SET = Path.home() / ".second-brain" / "retrieval-eval.json"
QUERY_WORDS = 2

QA_MODES = ("keyword", "semantic", "hybrid", "recall")
# The kinds of item a question's gold may name. An email counts at thread level: any
# email of its thread finds it. A thread is an Outlook conversation id.
GOLD_KINDS = ("email", "thread", "attachment", "teams", "whatsapp", "conversation", "event")
# What each mode can return. A question is graded in a mode on these kinds only, and
# not at all in a mode that can return none of its gold.
REACH = {
    "keyword": {"email", "thread"},
    "hybrid": {"email", "thread"},
    "semantic": {"email", "thread", "attachment", "teams", "whatsapp", "conversation"},
    "recall": set(GOLD_KINDS),
}
QA_SIZE = 60
TOP = 10
# recall's per-kind maximum: recall@10 there is a gold item among the first ten of its kind.
RECALL_PER_KIND = 10
MIN_QUESTION_CHARS = 12
MAX_QUESTION_CHARS = 600
MIN_QUESTION_WORDS = 3

# The id fields a recall bucket's rows carry, by gold kind. A decision, action or
# commitment row points at the item it came from.
BUCKET_IDS: dict[str, tuple[tuple[str, str], ...]] = {
    "emails": (("email", "email_id"),),
    "attachments": (("attachment", "attachment_id"),),
    "conversations": (("conversation", "conversation_id"), ("conversation", "session_id")),
    "decisions": (
        ("email", "email_id"),
        ("event", "event_id"),
        ("teams", "teams_thread_id"),
        ("whatsapp", "whatsapp_thread_id"),
    ),
    "actions": (("email", "email_id"),),
    "commitments": (("email", "email_id"),),
    "teams": (("teams", "thread_id"),),
    "whatsapp": (("whatsapp", "thread_id"),),
    "calendar_events": (("event", "id"),),
}
# The same for semantic search's rows, by their `type`. Its attachment rows name the
# extracted text's id, which _expand maps to the attachment's.
SEMANTIC_IDS: dict[str, tuple[tuple[str, str], ...]] = {
    "email": (("email", "email_id"),),
    "attachment": (("attachment_content", "attachment_content_id"),),
    "teams_thread": (("teams", "id"),),
    "whatsapp_thread": (("whatsapp", "id"),),
    "conversation": (("conversation", "id"), ("conversation", "session_id")),
}


def default_qa_file() -> Path:
    return eval_home() / "retrieval" / "qa.jsonl"


def default_results() -> Path:
    return eval_home() / "retrieval" / "results.jsonl"


def _open(db: Path) -> sqlite3.Connection:
    return connect_read_only(db)


def build_set(conn: sqlite3.Connection, size: int, seed: int) -> list[dict]:
    """`size` emails, each with a query of QUERY_WORDS words from its own subject that no
    other thread's subject holds both of.

    The longest words are kept: they are the ones someone remembers an email by, and the
    least likely to match half the mailbox. News is never an answer but is a competitor,
    as `run` searches every class.
    """
    from src.store.query import thread_keys

    rows = conn.execute(
        "SELECT id, subject, mailbox_name FROM emails WHERE COALESCE(subject, '') <> '' ORDER BY id"
    ).fetchall()
    threads = thread_keys(conn, [row["id"] for row in rows])
    holders: dict[str, set] = {}
    queries: dict[int, tuple[list[str], object]] = {}
    for row in rows:
        words = search_words(row["subject"])
        thread = threads.get(row["id"]) or ("email", row["id"])
        for word in words:
            holders.setdefault(word, set()).add(thread)
        if len(words) >= QUERY_WORDS and (row["mailbox_name"] or "") != "News":
            queries[row["id"]] = (sorted(words, key=lambda w: (-len(w), w))[:QUERY_WORDS], thread)
    usable = [
        email_id
        for email_id, (words, thread) in queries.items()
        if set.intersection(*(holders[w] for w in words)) == {thread}
    ]
    # Seeded on purpose: the same --seed has to give the same set, and a sample of the
    # owner's own emails guards no secret.
    picked = random.Random(seed).sample(usable, min(size, len(usable)))  # NOSONAR
    return [{"email_id": email_id, "query": " ".join(queries[email_id][0])} for email_id in picked]


def rank_of(results: list[dict], email_ids: set[int]) -> int | None:
    """1-based position of the first of `email_ids` in `results`, or None."""
    for n, row in enumerate(results, start=1):
        if row.get("email_id") in email_ids:
            return n
    return None


def thread_of(conn: sqlite3.Connection, email_id: int) -> set[int]:
    """The ids of every email in `email_id`'s thread, itself included: the thread
    search and email_thread know, so a News day or the blank-subject hash is not
    one."""
    from src.store.query import query_thread

    return {e["email_id"] for e in query_thread(conn, email_id, limit=-1)} | {email_id}


def score(ranks: list[int | None]) -> dict:
    """hit@1, hit@5, hit@10 and mean reciprocal rank over the ranks found."""
    total = len(ranks) or 1
    return {
        "queries": len(ranks),
        "hit@1": sum(1 for r in ranks if r == 1) / total,
        "hit@5": sum(1 for r in ranks if r is not None and r <= 5) / total,
        "hit@10": sum(1 for r in ranks if r is not None and r <= 10) / total,
        "mrr": sum(1 / r for r in ranks if r) / total,
    }


def run_set(conn: sqlite3.Connection, items: list[dict]) -> tuple[dict, list[dict]]:
    """Search every query as search_emails does; return the score and the misses."""
    from src.store.greek import register_sql_functions
    from src.store.query import query_by_keyword

    register_sql_functions(conn)
    ranks, thread_ranks, misses = [], [], []
    for item in items:
        # Every class, as before v33: a set built before it may hold the owner's
        # automation mail, which search_emails now leaves out unless asked, and a
        # class left out is no tokenising regression.
        results = query_by_keyword(
            conn, item["query"], limit=10, include_news=True, include_automation=True
        )
        rank = rank_of(results, {item["email_id"]})
        thread_rank = rank_of(results, thread_of(conn, item["email_id"]))
        ranks.append(rank)
        thread_ranks.append(thread_rank)
        if thread_rank != 1:
            misses.append({**item, "rank": thread_rank})
    return {"email": score(ranks), "thread": score(thread_ranks)}, misses


# --- the qa set ------------------------------------------------------------------------


def _own_repository(workspace: str | None, server: str) -> bool:
    """A session in the brain's own checkout or a worktree of it: development, not use."""
    return any(server in part for part in Path(workspace or "").parts)


def mine_questions(
    conn: sqlite3.Connection,
    size: int = QA_SIZE,
    *,
    server: str = "second-brain",
    all_workspaces: bool = False,
) -> list[dict]:
    """Up to `size` questions the owner asked that a brain tool answered, newest first.

    A question is the user turn just before an assistant turn whose tool markers name a
    tool of `server` (the MCP server's registered name). Tool notifications and commands
    (a turn opening with a tag), one-word turns and walls of pasted text are left out, and
    a question asked twice is kept once, at its newest.
    """
    marker = re.compile(rf"\[Tool: mcp__(?:[\w-]*?_)?{re.escape(server)}__(\w+)\]")
    rows = conn.execute(
        """
        SELECT u.content, u.timestamp, u.turn_index, a.content, c.session_id, c.workspace
        FROM conversation_turns a
        JOIN conversation_turns u
          ON u.conversation_id = a.conversation_id AND u.turn_index = a.turn_index - 1
        JOIN conversations c ON c.id = a.conversation_id
        WHERE a.speaker = 'assistant' AND u.speaker = 'user' AND a.content LIKE ?
        ORDER BY u.timestamp DESC, u.id DESC
        """,
        (f"%{server}%",),
    )
    seen: set[str] = set()
    out: list[dict] = []
    for content, asked_at, turn_index, tool_turn, session_id, workspace in rows:
        tools = sorted(set(marker.findall(tool_turn or "")))
        if not tools or (not all_workspaces and _own_repository(workspace, server)):
            continue
        text = " ".join((content or "").split())
        if (
            text.startswith("<")
            or not MIN_QUESTION_CHARS <= len(text) <= MAX_QUESTION_CHARS
            or len(text.split()) < MIN_QUESTION_WORDS
        ):
            continue
        key = search_fold(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": f"q{len(out) + 1:03d}",
                "question": text,
                "query": "",
                "tools": tools,
                "asked_at": asked_at,
                "source": {"session_id": session_id, "turn_index": turn_index},
                "modes": [],
                "gold": {kind: [] for kind in GOLD_KINDS},
                "answer": "",
            }
        )
        if len(out) >= size:
            break
    return out


# Kinds whose ids are the store's row numbers; a thread is an Outlook conversation id and
# a conversation may be named by its row number or its session id.
NUMERIC_KINDS = ("email", "attachment", "teams", "whatsapp", "event")


def _is_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_id(value) -> bool:
    return _is_number(value) or (isinstance(value, str) and bool(value.strip()))


def validate_qa(items: list[dict]) -> list[str]:
    """Every problem with the qa set, by question id. A message names the field, never
    the value written there: problems are printed, and the set stays in its file."""
    problems: list[str] = []
    ids: set[str] = set()
    for n, item in enumerate(items, start=1):
        qid = item.get("id") if isinstance(item.get("id"), str) and item.get("id") else None
        name = qid or f"line {n}"
        if qid is None:
            problems.append(f"{name}: no id")
        elif qid in ids:
            problems.append(f"{name}: duplicate id")
        ids.add(name)
        if not isinstance(item.get("question"), str) or not item["question"].strip():
            problems.append(f"{name}: no question")
        if not isinstance(item.get("query", ""), str):
            problems.append(f"{name}: query is not text")
        modes = item.get("modes") or []
        if not isinstance(modes, list) or not set(modes) <= set(QA_MODES):
            problems.append(f"{name}: modes may hold only {', '.join(QA_MODES)}")
        gold = item.get("gold", {})
        if not isinstance(gold, dict):
            problems.append(f"{name}: gold is not an object")
            continue
        if not set(gold) <= set(GOLD_KINDS):
            problems.append(f"{name}: gold may name only {', '.join(GOLD_KINDS)}")
        for kind in GOLD_KINDS:
            values = gold.get(kind, [])
            numeric = kind in NUMERIC_KINDS
            valid = _is_number if numeric else _is_id
            if not isinstance(values, list) or not all(valid(v) for v in values):
                what = "numeric ids" if numeric else "ids"
                problems.append(f"{name}: gold.{kind} is not a list of {what}")
    return problems


def is_labelled_qa(item: dict) -> bool:
    gold = item.get("gold")
    return isinstance(gold, dict) and any(gold.get(kind) for kind in GOLD_KINDS)


def _keys(row: dict, fields: tuple[tuple[str, str], ...]) -> set:
    return {(kind, row[field]) for kind, field in fields if row.get(field) is not None}


def _semantic_keys(row: dict) -> set:
    return _keys(row, SEMANTIC_IDS.get(str(row.get("type")), ()))


def _expand(conn: sqlite3.Connection, key_sets: list[set], email_ids: list) -> dict:
    """Add each email's thread to the keys that name it, turn an extracted text's id into
    its attachment's, and return the email -> thread map the gold units use."""
    from src.store.query import thread_keys

    emails = {v for keys in key_sets for kind, v in keys if kind == "email"} | set(email_ids)
    threads = thread_keys(conn, [e for e in emails if isinstance(e, int)])
    texts = [v for keys in key_sets for kind, v in keys if kind == "attachment_content"]
    attachment_of = dict(
        conn.execute(
            "SELECT id, attachment_id FROM attachment_content"
            " WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(texts),),
        ).fetchall()
        if texts
        else []
    )
    for keys in key_sets:
        for kind, value in list(keys):
            if kind == "email" and threads.get(value):
                keys.add(("thread", threads[value]))
            elif kind == "attachment_content":
                keys.discard((kind, value))
                if value in attachment_of:
                    keys.add(("attachment", attachment_of[value]))
    return threads


def _gold_units(gold: dict, threads: dict) -> list[tuple]:
    """The question's gold, one unit per thing to find: an email as its thread."""
    units: list[tuple] = []
    for kind in GOLD_KINDS:
        for value in gold.get(kind) or []:
            unit = (
                ("thread", threads[value])
                if kind == "email" and threads.get(value)
                else (kind, value)
            )
            if unit not in units:
                units.append(unit)
    return units


def _list_ranks(units: list[tuple], ranked: list[set]) -> list[int | None]:
    return [next((n for n, keys in enumerate(ranked, 1) if unit in keys), None) for unit in units]


def _bundle_ranks(units: list[tuple], buckets: dict[str, list[set]]) -> list[int | None]:
    """A unit's best place within any of recall's buckets."""
    ranks: list[int | None] = []
    for unit in units:
        places = [n for rows in buckets.values() for n, keys in enumerate(rows, 1) if unit in keys]
        ranks.append(min(places) if places else None)
    return ranks


def qa_metrics(ranks: list[int | None]) -> dict:
    """recall@5, recall@10, reciprocal rank and nDCG@10 of one question's gold units.

    Gains are binary, one per unit at the place it is first found. Capped at 1: in recall's
    bundle two units can share a place, one in each bucket.
    """
    found = [r for r in ranks if r is not None]
    n = len(ranks) or 1
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(len(ranks), TOP) + 1))
    dcg = sum(1 / math.log2(r + 1) for r in found if r <= TOP)
    return {
        "recall@5": sum(r <= 5 for r in found) / n,
        "recall@10": sum(r <= TOP for r in found) / n,
        "rr": 1 / min(found) if found else 0.0,
        "ndcg@10": min(1.0, dcg / ideal) if ideal else 0.0,
    }


def _semantic_rows(conn, query: str, embed_fn, index_path) -> tuple[list[dict], str]:
    """search_emails' semantic mode, and how its query vector was made. An outage is a
    result here: it comes back as the status, with no rows."""
    from src.store.embeddings import query_semantic

    try:
        rows = query_semantic(conn, query, limit=TOP, embed_fn=embed_fn, index_path=index_path)
    except Exception as e:  # an index or a model that is down is what this records
        return [], f"unavailable: {type(e).__name__}"
    seeded = next((r["semantic"] for r in rows if r.get("semantic")), None)
    return rows, seeded or "ok"


def run_qa(conn: sqlite3.Connection, items: list[dict], *, embed_fn=None, index_path=None) -> dict:
    """Every labelled question in every mode it allows, graded per mode.

    `embed_fn` and `index_path` reach the embedding store as the tools' own injection
    points; left None, the configured backend and index are used, as the tools use them.
    """
    from src.store.embeddings import semantic_email_candidates
    from src.store.greek import register_sql_functions
    from src.store.query import query_by_keyword
    from src.store.recall import recall

    register_sql_functions(conn)
    candidates = partial(semantic_email_candidates, embed_fn=embed_fn, index_path=index_path)
    status: dict[str, Counter] = {"semantic": Counter(), "recall": Counter()}
    graded: dict[str, list[dict]] = {mode: [] for mode in QA_MODES}
    per_question = []
    unknown = 0
    labelled = [item for item in items if is_labelled_qa(item)]
    for item in labelled:
        query = (item.get("query") or "").strip() or item["question"]
        semantic, semantic_status = _semantic_rows(conn, query, embed_fn, index_path)
        bundle = recall(conn, query, limit_per_kind=RECALL_PER_KIND, semantic_candidates=candidates)
        recall_status = bundle["summary"].get("semantic", "unavailable")
        status["semantic"][semantic_status] += 1
        status["recall"][recall_status] += 1
        email_ids = BUCKET_IDS["emails"]
        lists = {
            "keyword": [_keys(r, email_ids) for r in query_by_keyword(conn, query, limit=TOP)],
            "semantic": [_semantic_keys(r) for r in semantic],
            "hybrid": [_keys(r, email_ids) for r in bundle["emails"][:TOP]],
        }
        buckets = {
            name: [_keys(r, fields) for r in bundle.get(name) or []]
            for name, fields in BUCKET_IDS.items()
        }
        gold_emails = list(item["gold"].get("email") or [])
        threads = _expand(
            conn,
            [keys for rows in [*lists.values(), *buckets.values()] for keys in rows],
            gold_emails,
        )
        unknown += sum(1 for e in gold_emails if e not in threads)
        units = _gold_units(item["gold"], threads)
        modes = [m for m in (item.get("modes") or QA_MODES) if m in QA_MODES]
        result: dict = {
            "id": item["id"],
            "units": [f"{kind}:{value}" for kind, value in units],
            "semantic": semantic_status,
            "recall_semantic": recall_status,
            "modes": {},
        }
        for mode in modes:
            reachable = [u for u in units if u[0] in REACH[mode]]
            if not reachable:
                continue
            ranks = (
                _bundle_ranks(reachable, buckets)
                if mode == "recall"
                else _list_ranks(reachable, lists[mode])
            )
            metrics = qa_metrics(ranks)
            result["modes"][mode] = {"ranks": ranks, **metrics}
            graded[mode].append(metrics)
        per_question.append(result)

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    return {
        "questions": len(items),
        "labelled": len(labelled),
        "unknown_gold": unknown,
        "modes": {
            mode: {
                "questions": len(rows),
                "recall@5": mean([r["recall@5"] for r in rows]),
                "recall@10": mean([r["recall@10"] for r in rows]),
                "mrr": mean([r["rr"] for r in rows]),
                "ndcg@10": mean([r["ndcg@10"] for r in rows]),
            }
            for mode, rows in graded.items()
            if rows
        },
        "semantic_status": {name: dict(counts) for name, counts in status.items()},
        "per_question": per_question,
    }


# --- the command line ----------------------------------------------------------------


def _known_item(args) -> int:
    conn = _open(args.db)
    try:
        if args.command == "build":
            items = build_set(conn, args.size or 20, args.seed)
            write_private(args.file, json.dumps(items, ensure_ascii=False, indent=2))
            print(f"{len(items)} queries written to {args.file}")
            return 0
        items = json.loads(args.file.read_text(encoding="utf-8"))
        result, misses = run_set(conn, items)
    finally:
        conn.close()
    append_jsonl(
        args.results,
        {
            "eval": "known-item",
            **stamp(),
            "set_sha": file_sha(args.file),
            **result,
            "misses": len(misses),
        },
    )
    print(json.dumps(result, indent=2))
    for miss in misses:
        print(f"  thread rank {miss['rank']}: email {miss['email_id']}")
    print(f"recorded in {args.results}")
    return 0


def _build_qa(args) -> int:
    if args.qa_file.exists() and not args.force:
        print(
            f"{args.qa_file} exists and may hold your labels; pass --force to replace it.",
            file=sys.stderr,
        )
        return 2
    conn = _open(args.db)
    try:
        items = mine_questions(
            conn, args.size or QA_SIZE, server=args.server, all_workspaces=args.all_workspaces
        )
    finally:
        conn.close()
    write_jsonl(args.qa_file, items)
    print(
        f"{len(items)} candidate questions written to {args.qa_file}. Keep the real ones, fill"
        " each one's gold ids (and, if you like, the keywords a session would send as `query`),"
        " then run --run-qa."
    )
    return 0


def _run_qa(args) -> int:
    if not args.qa_file.exists():
        print(f"no qa set at {args.qa_file}: run --build-qa first", file=sys.stderr)
        return 2
    try:
        items = read_jsonl(args.qa_file)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    problems = validate_qa(items)
    for problem in problems:
        print(f"  problem: {problem}")
    if problems:
        return 1
    if not any(is_labelled_qa(item) for item in items):
        print(f"no question in {args.qa_file} has gold ids yet: label some first", file=sys.stderr)
        return 2
    conn = _open(args.db)
    try:
        result = run_qa(conn, items)
    finally:
        conn.close()
    append_jsonl(
        args.results, {"eval": "qa", **stamp(), "set_sha": file_sha(args.qa_file), **result}
    )
    print(f"{result['labelled']} of {result['questions']} questions labelled and asked")
    for mode, row in result["modes"].items():
        print(
            f"  {mode:<9} {row['questions']:>3} questions  recall@5 {row['recall@5']:.2f}"
            f"  recall@10 {row['recall@10']:.2f}  MRR {row['mrr']:.2f}  nDCG@10 {row['ndcg@10']:.2f}"
        )
    for name, counts in result["semantic_status"].items():
        print(f"  {name} calls: " + ", ".join(f"{s} {n}" for s, n in sorted(counts.items())))
    if result["unknown_gold"]:
        print(f"  {result['unknown_gold']} gold email ids are not in the store")
    print(f"recorded in {args.results}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", nargs="?", choices=["build", "run"], help="the known-item eval")
    parser.add_argument(
        "--build-qa", action="store_true", help="draft the qa set from your sessions"
    )
    parser.add_argument("--run-qa", action="store_true", help="grade the labelled qa set")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--file", type=Path, default=DEFAULT_SET, help="the known-item set")
    parser.add_argument("--qa-file", type=Path, default=None, help="the qa set")
    parser.add_argument("--results", type=Path, default=None, help="where runs are recorded")
    parser.add_argument("--size", type=int, default=None, help="20 known items, 60 questions")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--force", action="store_true", help="let --build-qa replace a set")
    parser.add_argument(
        "--all-workspaces",
        action="store_true",
        help="mine the brain's own repository sessions too",
    )
    parser.add_argument("--server", default="second-brain", help="the MCP server's name")
    args = parser.parse_args(argv)
    if sum(map(bool, (args.command, args.build_qa, args.run_qa))) != 1:
        parser.error("choose one of: build, run, --build-qa, --run-qa")
    args.qa_file = args.qa_file or default_qa_file()
    args.results = args.results or default_results()

    written = {"build": args.file, "run": args.results}.get(args.command or "")
    for path in (written, args.qa_file if args.build_qa else None, args.results):
        if path is not None and inside_repo(path):
            print(f"refusing {path}: eval sets and results never go inside the repository")
            return 2
    if args.command:
        return _known_item(args)
    return _build_qa(args) if args.build_qa else _run_qa(args)


if __name__ == "__main__":
    sys.exit(main())

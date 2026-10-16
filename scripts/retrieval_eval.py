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
can return, overall and by the question's shape. The same again with the recency
ranking (_recency), and with recall's entity list too (_entity: hybrid_entity,
recall_entity), side by side. It records the semantic status of every semantic and
recall call, so an embedding outage shows, the time of each recall call, and the space
of the index searched (`--index`, default the store's): model and dimension. Its only
model calls are query embeddings.

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
import os
import random
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
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

# The plain modes rank as the code before the recency ranking did (BRAIN_QUERY_RANKING
# relevance, BRAIN_QUERY_NAMES 0), so a run compares with the runs before it; each
# _recency mode is the same search with it: recency fused, editions collapsed, names in
# both scripts, every recall bucket fused, thread siblings collapsed before the cut.
RANKED_MODES = {
    "keyword_recency": "keyword",
    "hybrid_recency": "hybrid",
    "recall_recency": "recall",
}
# The _recency modes run without recall's entity list (BRAIN_RECALL_ENTITY_LIST 0), so
# they compare with the runs before it, whatever its default; each _entity mode is the
# same recall with it (src/store/recall.py ENTITY_WEIGHT).
ENTITY_MODES = {"hybrid_entity": "hybrid", "recall_entity": "recall"}
QA_MODES = ("keyword", "semantic", "hybrid", "recall", *RANKED_MODES, *ENTITY_MODES)
# The modes a row stands for its series in (`editions`).
SERIES_MODES = (*RANKED_MODES, *ENTITY_MODES)
# The recall bundles of a question, each a call to time.
BUNDLE_MODES = ("recall", "recall_recency", "recall_entity")
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
REACH.update({mode: REACH[plain] for mode, plain in {**RANKED_MODES, **ENTITY_MODES}.items()})
# The modes an unanswerable question (expect_empty) is checked in: a row there that holds
# every query word claims an answer. Semantic search always returns its nearest rows.
ABSTAINING_MODES = tuple(m for m in QA_MODES if m != "semantic")
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
        problems += _id_lists(name, "gold", gold)
        alt = item.get("alt", {})
        if not isinstance(alt, dict) or not set(alt) <= set(GOLD_KINDS):
            problems.append(f"{name}: alt is not an object of {', '.join(GOLD_KINDS)}")
        else:
            problems += _id_lists(name, "alt", alt)
        if not isinstance(item.get("expect_empty", False), bool):
            problems.append(f"{name}: expect_empty is not true or false")
        if not isinstance(item.get("shape", ""), str):
            problems.append(f"{name}: shape is not text")
    return problems


def _id_lists(name: str, field: str, ids: dict) -> list[str]:
    problems = []
    for kind in GOLD_KINDS:
        values = ids.get(kind, [])
        numeric = kind in NUMERIC_KINDS
        valid = _is_number if numeric else _is_id
        if not isinstance(values, list) or not all(valid(v) for v in values):
            what = "numeric ids" if numeric else "ids"
            problems.append(f"{name}: {field}.{kind} is not a list of {what}")
    return problems


def is_labelled_qa(item: dict) -> bool:
    gold = item.get("gold")
    return isinstance(gold, dict) and any(gold.get(kind) for kind in GOLD_KINDS)


def _keys(row: dict, fields: tuple[tuple[str, str], ...], editions: bool = True) -> set:
    """The (kind, id) units a row finds: its own ids, and with `editions` the items of its
    series it stands for (src/store/ranking.py), which a reader has the refs of."""
    keys = {(kind, row[field]) for kind, field in fields if row.get(field) is not None}
    if editions and fields:
        keys |= {(fields[0][0], ref) for ref in row.get("editions") or ()}
    return keys


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


# The settings the plain modes run with, as the code before the recency ranking ranked;
# the _recency modes run with the opposite, without the entity list, and the _entity modes
# with it. Set for the run, then put back.
_PLAIN = {"BRAIN_QUERY_RANKING": "relevance", "BRAIN_QUERY_NAMES": "0"}
_RANKED = {
    "BRAIN_QUERY_RANKING": "fused",
    "BRAIN_QUERY_NAMES": "1",
    "BRAIN_RECALL_ENTITY_LIST": "0",
}
_ENTITY = {**_RANKED, "BRAIN_RECALL_ENTITY_LIST": "1"}


@contextmanager
def _ranking(settings: dict[str, str]):
    saved = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _memo(embed_fn):
    """`embed_fn` (the configured one when None) asked once per query text: every mode of
    a question shares its vector, so a run costs one embedding a question. A failure is
    not kept, so each mode records it."""
    from src.store.embeddings import embed_query

    embed = embed_fn or embed_query
    seen: dict[tuple, object] = {}

    def memo(texts):
        key = tuple(texts)
        if key not in seen:
            seen[key] = embed(texts)
        return seen[key]

    return memo


def _group(unit: tuple) -> str:
    """The kind a unit is graded as: an email and its thread are one."""
    return "email" if unit[0] in ("email", "thread") else unit[0]


def _with_alt(units: list, ranks: list, alt_units: list, alt_ranks: list) -> list:
    """The units' ranks when a found alt (a copy or version of a gold item) stands for a
    gold unit of its kind not found sooner: each kind's best places, own or alt, go to
    its units."""
    out = list(ranks)
    for group in {_group(u) for u in units}:
        slots = [i for i, u in enumerate(units) if _group(u) == group]
        found = [ranks[i] for i in slots if ranks[i] is not None]
        found += [r for u, r in zip(alt_units, alt_ranks, strict=True) if _group(u) == group and r]
        found.sort()
        for n, i in enumerate(slots):
            out[i] = found[n] if n < len(found) else None
    return out


# The tags of a row no query word matched: semantic search or the entity list alone
# brought it (src/store/envelope.py).
_UNMATCHED = frozenset({"semantic", "entity"})


def _answered(rows: Iterable[dict]) -> bool:
    """Whether a list holds a row that claims an answer: one holding every query word,
    not a partial, a semantic-only or an entity-only row."""
    return any(
        not r.get("partial_match") and not {r.get("source"), r.get("matched_in")} & _UNMATCHED
        for r in rows
    )


def _ranked(mode: str, bundles: dict, rows: dict, editions: bool = True):
    """What `mode` returned, as the units each row finds: a list for the list modes, a
    list per bucket for the recall modes."""
    if mode in bundles:
        return {
            name: [_keys(r, fields, editions) for r in bundles[mode].get(name) or []]
            for name, fields in BUCKET_IDS.items()
        }
    if mode == "semantic":
        return [_semantic_keys(r) for r in rows[mode]]
    return [_keys(r, BUCKET_IDS["emails"], editions) for r in rows[mode]]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _summary(graded: dict[str, list[dict]]) -> dict:
    return {
        mode: {
            "questions": len(rows),
            "recall@5": _mean([r["recall@5"] for r in rows]),
            "recall@10": _mean([r["recall@10"] for r in rows]),
            "mrr": _mean([r["rr"] for r in rows]),
            "ndcg@10": _mean([r["ndcg@10"] for r in rows]),
        }
        for mode, rows in graded.items()
        if rows
    }


def _timed(call):
    """call() and the milliseconds it took."""
    started = time.perf_counter()
    out = call()
    return out, (time.perf_counter() - started) * 1000


def _latency(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "calls": len(ordered),
        "median": round(statistics.median(ordered), 1),
        "p90": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1),
    }


def run_qa(conn: sqlite3.Connection, items: list[dict], *, embed_fn=None, index_path=None) -> dict:
    """Every labelled question in every mode it allows, graded per mode.

    `embed_fn` and `index_path` reach the embedding store as the tools' own injection
    points; left None, the configured backend and index are used, as the tools use them.
    `index` is the space of the index searched (embeddings.file_space): the model and
    dimension the run measured. The plain modes run as the code before the recency
    ranking (_PLAIN), the _recency modes with it but without recall's entity list
    (_RANKED), the _entity modes with that too (_ENTITY), each question embedded once for
    all of them; `latency_ms` times each recall call, the plain one paying the embedding.
    Graded twice:
    strictly on the gold, comparable with earlier runs, and in `modes_with_alt` with a
    question's `alt` ids (copies, versions) standing for its gold. A gold item a row
    lists among its `editions` is found at that row, and `found_in_editions` counts
    them. Questions marked expect_empty are checked for `abstention`.
    """
    from src.store.embeddings import file_space, semantic_email_candidates
    from src.store.greek import register_sql_functions
    from src.store.query import query_by_keyword
    from src.store.recall import recall

    register_sql_functions(conn)
    embed = _memo(embed_fn)
    candidates = partial(semantic_email_candidates, embed_fn=embed, index_path=index_path)
    status: dict[str, Counter] = {"semantic": Counter(), **{m: Counter() for m in BUNDLE_MODES}}
    took: dict[str, list[float]] = {mode: [] for mode in BUNDLE_MODES}
    graded: dict[str, list[dict]] = {mode: [] for mode in QA_MODES}
    graded_alt: dict[str, list[dict]] = {mode: [] for mode in QA_MODES}
    by_shape: dict[str, dict[str, list[dict]]] = {}
    in_editions: Counter = Counter()
    abstention = {mode: {"cases": 0, "abstained": 0} for mode in ABSTAINING_MODES}
    per_question = []
    unknown = 0
    labelled = [item for item in items if is_labelled_qa(item)]
    unanswerable = [item for item in items if item.get("expect_empty") and not is_labelled_qa(item)]
    for item in [*labelled, *unanswerable]:
        query = (item.get("query") or "").strip() or item["question"]
        ask = partial(
            recall, conn, query, limit_per_kind=RECALL_PER_KIND, semantic_candidates=candidates
        )
        bundles: dict[str, dict] = {}
        with _ranking(_PLAIN):
            semantic, semantic_status = _semantic_rows(conn, query, embed, index_path)
            bundles["recall"], ms = _timed(ask)
            took["recall"].append(ms)
            keyword = {"keyword": query_by_keyword(conn, query, limit=TOP)}
        with _ranking(_RANKED):
            bundles["recall_recency"], ms = _timed(ask)
            took["recall_recency"].append(ms)
            keyword["keyword_recency"] = query_by_keyword(conn, query, limit=TOP)
        with _ranking(_ENTITY):
            bundles["recall_entity"], ms = _timed(ask)
            took["recall_entity"].append(ms)
        rows = {
            **keyword,
            "semantic": semantic,
            "hybrid": bundles["recall"]["emails"][:TOP],
            "hybrid_recency": bundles["recall_recency"]["emails"][:TOP],
            "hybrid_entity": bundles["recall_entity"]["emails"][:TOP],
        }
        if item in unanswerable:
            for mode in ABSTAINING_MODES:
                bundle = bundles.get(mode)
                shown = (
                    [r for b in BUCKET_IDS for r in bundle.get(b) or []] if bundle else rows[mode]
                )
                abstention[mode]["cases"] += 1
                abstention[mode]["abstained"] += not _answered(shown)
            continue
        status["semantic"][semantic_status] += 1
        for name in BUNDLE_MODES:
            status[name][bundles[name]["summary"].get("semantic", "unavailable")] += 1

        lists = {mode: _ranked(mode, bundles, rows) for mode in QA_MODES}
        # The same without the editions rows stand for, to count what only they found.
        own = {mode: _ranked(mode, bundles, rows, editions=False) for mode in SERIES_MODES}
        gold_emails = list(item["gold"].get("email") or [])
        alt = item.get("alt") or {}
        every_key = [
            keys
            for found in [*lists.values(), *own.values()]
            for keys in (
                found if isinstance(found, list) else [k for ks in found.values() for k in ks]
            )
        ]
        threads = _expand(conn, every_key, gold_emails + list(alt.get("email") or []))
        unknown += sum(1 for e in gold_emails if e not in threads)
        units = _gold_units(item["gold"], threads)
        alt_units = [u for u in _gold_units(alt, threads) if u not in units]
        modes = [m for m in (item.get("modes") or QA_MODES) if m in QA_MODES]
        result: dict = {
            "id": item["id"],
            "units": [f"{kind}:{value}" for kind, value in units],
            "semantic": semantic_status,
            "recall_semantic": bundles["recall"]["summary"].get("semantic", "unavailable"),
            "modes": {},
        }

        def ranks_of(mode: str, wanted: list, found) -> list:
            return (
                _bundle_ranks(wanted, found)
                if isinstance(found, dict)
                else _list_ranks(wanted, found)
            )

        for mode in modes:
            reachable = [u for u in units if u[0] in REACH[mode]]
            if not reachable:
                continue
            ranks = ranks_of(mode, reachable, lists[mode])
            if mode in SERIES_MODES:
                plain = ranks_of(mode, reachable, own[mode])
                in_editions[mode] += sum(
                    1 for r, p in zip(ranks, plain, strict=True) if r is not None and p is None
                )
            metrics = qa_metrics(ranks)
            result["modes"][mode] = {"ranks": ranks, **metrics}
            graded[mode].append(metrics)
            alt_reach = [u for u in alt_units if u[0] in REACH[mode]]
            alt_ranks = ranks_of(mode, alt_reach, lists[mode])
            graded_alt[mode].append(qa_metrics(_with_alt(reachable, ranks, alt_reach, alt_ranks)))
            shape = item.get("shape") or "unlabelled"
            by_shape.setdefault(shape, {}).setdefault(mode, []).append(metrics)
        per_question.append(result)

    return {
        "questions": len(items),
        "labelled": len(labelled),
        "unknown_gold": unknown,
        "modes": _summary(graded),
        "modes_with_alt": _summary(graded_alt),
        "found_in_editions": dict(in_editions),
        "abstention": {mode: counts for mode, counts in abstention.items() if counts["cases"]},
        "by_shape": {
            shape: {
                mode: {
                    "questions": len(values),
                    "recall@10": _mean([v["recall@10"] for v in values]),
                    "mrr": _mean([v["rr"] for v in values]),
                }
                for mode, values in modes.items()
            }
            for shape, modes in by_shape.items()
        },
        "semantic_status": {name: dict(counts) for name, counts in status.items()},
        "latency_ms": {mode: _latency(values) for mode, values in took.items() if values},
        "index": file_space(index_path),
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
        result = run_qa(conn, items, index_path=args.index)
    finally:
        conn.close()
    append_jsonl(
        args.results, {"eval": "qa", **stamp(), "set_sha": file_sha(args.qa_file), **result}
    )
    print(f"{result['labelled']} of {result['questions']} questions labelled and asked")
    space = result["index"] or {}
    print(f" index: {space.get('model')} at {space.get('dim')} dimensions")
    for title, table in (("gold", result["modes"]), ("gold or alt", result["modes_with_alt"])):
        print(f" {title}:")
        for mode, row in table.items():
            print(
                f"  {mode:<15} {row['questions']:>3} questions  recall@5 {row['recall@5']:.2f}"
                f"  recall@10 {row['recall@10']:.2f}  MRR {row['mrr']:.2f}"
                f"  nDCG@10 {row['ndcg@10']:.2f}"
            )
    for mode, n in result["found_in_editions"].items():
        print(f"  {mode}: {n} gold items found only in a row's editions")
    for mode, counts in result["abstention"].items():
        print(f"  {mode}: abstained on {counts['abstained']} of {counts['cases']} unanswerable")
    print(" recall@10 / MRR by shape:")
    for shape, modes in sorted(result["by_shape"].items()):
        cells = "  ".join(f"{m} {v['recall@10']:.2f}/{v['mrr']:.2f}" for m, v in modes.items())
        print(f"  {shape:<14} {cells}")
    for mode, row in result["latency_ms"].items():
        print(f"  {mode} call: median {row['median']:.0f} ms, p90 {row['p90']:.0f} ms")
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
    parser.add_argument(
        "--index", type=Path, default=None, help="the vector index (default: the store's)"
    )
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

"""Topic registry (issue 180, gated step `topic_registry`): canonical topics and their aliases.

The topics table holds every name the model ever wrote (146k on 2026-11-04, most used
once), so one subject sits under many rows. The registry adds a layer above it and
changes no topic and no link:

  canonical_topics  one row a cluster: its name (normalized, as topics.name), the name
                    shown, who named it ('llm' for a cluster of several names, 'rule':
                    the most used member's name) and its head, the member it is
                    compared by
  topic_aliases     every raw topic placed in a cluster: its canonical, how ('fold':
                    the same words once plurals and order are folded; 'embedding': a
                    name within the floor of the head's vector holding the same numbers;
                    'load': mapped as the loader created it, by its folded words), the
                    score, and its fold key
  teams_thread_topics, whatsapp_thread_topics, event_topics
                    the topics of a Teams thread, a WhatsApp session and a calendar
                    event, which until now had none

Clusters are built in batches (`brain maint topic-registry`, a dry run unless --apply):
the most used topics not yet placed, `--limit` a run, each joining a canonical whose folded
words it shares, else the nearest head by embedding within the floor, else a new cluster
of its own (with the topics of the batch near it). The owner runs it nightly until every
topic is placed; a topic created after that is mapped at load (map_new_topic) when its
folded words match an alias, and waits for the next batch otherwise.

A store without the step: alias_ids is the topic alone, canonical_of None, link_topics
does nothing, and every read is as before.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.store import embeddings
from src.store.normalizer import find_or_create_topic, normalize_topic
from src.store.topic_canon import _words, topic_uses

TABLES = (
    "canonical_topics",
    "topic_aliases",
    "teams_thread_topics",
    "whatsapp_thread_topics",
    "event_topics",
)

DDL = (
    """CREATE TABLE IF NOT EXISTS canonical_topics (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        named_by TEXT NOT NULL CHECK (named_by IN ('llm', 'rule')),
        head_topic_id INTEGER NOT NULL REFERENCES topics(id),
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_canonical_topics_name ON canonical_topics(name)",
    """CREATE TABLE IF NOT EXISTS topic_aliases (
        topic_id INTEGER PRIMARY KEY REFERENCES topics(id),
        canonical_id INTEGER NOT NULL REFERENCES canonical_topics(id),
        method TEXT NOT NULL CHECK (method IN ('head', 'fold', 'embedding', 'load')),
        score REAL NOT NULL,
        fold_key TEXT NOT NULL,
        mapped_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_topic_aliases_canonical ON topic_aliases(canonical_id)",
    "CREATE INDEX IF NOT EXISTS idx_topic_aliases_fold ON topic_aliases(fold_key)",
    """CREATE TABLE IF NOT EXISTS teams_thread_topics (
        thread_id INTEGER NOT NULL REFERENCES teams_threads(id),
        topic_id INTEGER NOT NULL REFERENCES topics(id),
        PRIMARY KEY (thread_id, topic_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_teams_thread_topics_topic ON teams_thread_topics(topic_id)",
    """CREATE TABLE IF NOT EXISTS whatsapp_thread_topics (
        thread_id INTEGER NOT NULL REFERENCES whatsapp_threads(id),
        topic_id INTEGER NOT NULL REFERENCES topics(id),
        PRIMARY KEY (thread_id, topic_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_thread_topics_topic"
    " ON whatsapp_thread_topics(topic_id)",
    """CREATE TABLE IF NOT EXISTS event_topics (
        event_id INTEGER NOT NULL REFERENCES calendar_events(id),
        topic_id INTEGER NOT NULL REFERENCES topics(id),
        PRIMARY KEY (event_id, topic_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_event_topics_topic ON event_topics(topic_id)",
)

# Source -> (link table, parent column).
LINKS = {
    "teams": ("teams_thread_topics", "thread_id"),
    "whatsapp": ("whatsapp_thread_topics", "thread_id"),
    "calendar": ("event_topics", "event_id"),
}

# Clusters named a model call, and the most names of a cluster shown to it.
NAME_BATCH = 40
NAME_MEMBERS = 12
NAME_MAX_TOKENS = 4_000
# Rows of the batch-by-heads similarity computed at once.
SIM_BLOCK = 512


# Topic names are embedded in a space of their own: the store's embedding model (prompted,
# task 'clustering'), 256 dimensions, cached by name in VECTORS_FILE with the space beside
# them, so a model change starts the cache afresh instead of mixing spaces. Not topic_canon's
# cache: that one holds gemini-embedding-001 vectors, and since the store moved to
# gemini-embedding-2 (which makes one vector of a plain list of strings) its embed_names
# no longer gives a vector a name.
TOPIC_DIM = 256
TOPIC_TASK = "clustering"
TOPIC_TEMPLATE = "topic-1"
VECTORS_FILE = "topic_registry_vectors.npz"
# The cosine floor in that space. Measured on a copy of the store of 2026-11-04: the 3,000
# most used names, each to its nearest more used one, 14 pairs a band judged by hand: 0.985
# and over 14 of 14 one topic, 0.98-0.985 12 of 14 (a narrower topic, and another quarter,
# which the number rule refuses), 0.975-0.98 11 of 14, 0.97-0.975 9 of 14, 0.95-0.96 under
# half ('staff training' and 'staff management'). The median nearest pair is at 0.936, so
# topic_canon's 0.92, measured on gemini-embedding-001, would merge most names.
TOPIC_FLOOR = 0.98


def topic_space() -> dict:
    return {
        "model": embeddings.EMBEDDING_MODEL,
        "dim": TOPIC_DIM,
        "task": TOPIC_TASK,
        "template": TOPIC_TEMPLATE,
    }


def embed_topic_names(names: list[str]):
    """Unit vectors of topic names in topic_space(), through embeddings.generate_embeddings
    (the store's client, batching, retries and row check)."""
    import numpy as np

    space = topic_space()
    rows = embeddings.generate_embeddings(
        [embeddings.query_text(space, n) for n in names], space=space
    )
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)


def cached_topic_vectors(path, embed: Callable = embed_topic_names) -> Callable:
    """vectors_of with a file cache keyed by name, kept only while its space is the
    current one: a name embedded before is not sent again."""
    import numpy as np

    space = json.dumps(topic_space(), sort_keys=True)

    def vectors_of(names):
        cache: dict[str, object] = {}
        if path.exists():
            data = np.load(path, allow_pickle=False)
            if str(data["space"]) == space:
                cache = dict(zip(data["names"].tolist(), data["vectors"], strict=True))
        missing = [n for n in dict.fromkeys(names) if n not in cache]
        if missing:
            cache.update(zip(missing, embed(missing), strict=True))
            keys = list(cache)
            np.savez(
                path,
                names=np.array(keys),
                vectors=np.stack([cache[k] for k in keys]),
                space=np.array(space),
            )
        if not names:
            return np.empty((0, TOPIC_DIM), dtype=np.float32)
        return np.stack([cache[n] for n in names])

    return vectors_of


def migrate(conn: sqlite3.Connection) -> None:
    """The gated step: the five tables, empty. No topic or link changes."""
    for statement in DDL:
        conn.execute(statement)


def enabled(conn: sqlite3.Connection) -> bool:
    """Whether the store took the step."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'topic_aliases'"
        ).fetchone()
        is not None
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fold_key(name: str) -> str:
    """A topic's content words, folded (topic_canon._words), sorted: '' for none."""
    return " ".join(sorted(_words(name)))


def _numbers(name: str) -> frozenset[str]:
    return frozenset(w for w in _words(name) if any(c.isdigit() for c in w))


def canonical_of(conn: sqlite3.Connection, topic_id: int) -> int | None:
    """The canonical topic a raw topic is an alias of; None without the step or unplaced."""
    if not enabled(conn):
        return None
    row = conn.execute(
        "SELECT canonical_id FROM topic_aliases WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    return row[0] if row else None


def alias_ids(conn: sqlite3.Connection, topic_id: int) -> list[int]:
    """The topic and every topic aliased to its canonical: itself first, then by id."""
    canonical = canonical_of(conn, topic_id)
    if canonical is None:
        return [topic_id]
    others = {
        r[0]
        for r in conn.execute(
            "SELECT topic_id FROM topic_aliases WHERE canonical_id = ?", (canonical,)
        )
    }
    return [topic_id] + sorted(others - {topic_id})


def canonical_name(conn: sqlite3.Connection, topic_id: int) -> str | None:
    """The shown name of the topic's canonical, or None."""
    canonical = canonical_of(conn, topic_id)
    if canonical is None:
        return None
    row = conn.execute(
        "SELECT display_name FROM canonical_topics WHERE id = ?", (canonical,)
    ).fetchone()
    return row[0] if row else None


def head_for_name(conn: sqlite3.Connection, normalized: str) -> int | None:
    """The head topic of the canonical named `normalized` exactly, or None."""
    if not enabled(conn) or not normalized:
        return None
    row = conn.execute(
        "SELECT head_topic_id FROM canonical_topics WHERE name = ? ORDER BY id LIMIT 1",
        (normalized,),
    ).fetchone()
    return row[0] if row else None


def aliases_of_sql() -> str:
    """A SELECT of the topic ids aliased to the canonical of any topic in `{ids}` (an
    inner SELECT of ids): to put after an IN, on a store with the step."""
    return (
        "SELECT a2.topic_id FROM topic_aliases a1 JOIN topic_aliases a2"
        " ON a2.canonical_id = a1.canonical_id WHERE a1.topic_id IN ({ids})"
    )


# --- map at load -------------------------------------------------------------------------------


def map_new_topic(conn: sqlite3.Connection, topic_id: int, name: str) -> int | None:
    """Place a topic the loader just created: under the canonical of an alias with the same
    folded words, or of a canonical with its name. The canonical id, or None (the next
    batch places it). A no-op without the step."""
    if not enabled(conn):
        return None
    key = fold_key(name)
    row = None
    if key:
        row = conn.execute(
            "SELECT canonical_id FROM topic_aliases WHERE fold_key = ?"
            " GROUP BY canonical_id ORDER BY COUNT(*) DESC, canonical_id LIMIT 1",
            (key,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM canonical_topics WHERE name = ? ORDER BY id LIMIT 1",
            (normalize_topic(name),),
        ).fetchone()
    if row is None:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO topic_aliases"
        " (topic_id, canonical_id, method, score, fold_key, mapped_at)"
        " VALUES (?, ?, 'load', 1.0, ?, ?)",
        (topic_id, row[0], key, _now()),
    )
    return row[0]


def link_topics(conn: sqlite3.Connection, source: str, parent_id: int, names: Iterable) -> int:
    """Tag a Teams thread, WhatsApp session or calendar event (`source` 'teams',
    'whatsapp', 'calendar') with topics, created (and mapped) as the loader creates them.
    The links added; 0 without the step. The caller commits."""
    if not enabled(conn):
        return 0
    table, column = LINKS[source]
    added = 0
    for name in names or ():
        if not isinstance(name, str) or not normalize_topic(name):
            continue
        topic_id = find_or_create_topic(conn, name.strip())
        added += conn.execute(
            f"INSERT OR IGNORE INTO {table} ({column}, topic_id) VALUES (?, ?)",
            (parent_id, topic_id),
        ).rowcount
    return added


# --- clustering in batches ---------------------------------------------------------------------


@dataclass
class Cluster:
    """A new canonical topic: its members (most used first; the first is the head) and how
    each joined."""

    members: list[int]
    methods: dict[int, tuple[str, float]] = field(default_factory=dict)
    name: str | None = None
    display_name: str | None = None
    named_by: str = "rule"


@dataclass
class Plan:
    """What a batch writes: topics joining a canonical already held, and new clusters."""

    joins: list[tuple[int, int, str, float]] = field(default_factory=list)
    clusters: list[Cluster] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)
    display: dict[int, str] = field(default_factory=dict)
    uses: dict[int, int] = field(default_factory=dict)
    llm_calls: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "joined": len(self.joins),
            "clusters": len(self.clusters),
            "placed": len(self.joins) + sum(len(c.members) for c in self.clusters),
            "llm_named": sum(c.named_by == "llm" for c in self.clusters),
            "llm_calls": self.llm_calls,
        }


def _unplaced(conn: sqlite3.Connection, uses: dict[int, int], limit: int) -> list[int]:
    placed = {r[0] for r in conn.execute("SELECT topic_id FROM topic_aliases")}
    ids = [t for t, n in uses.items() if n > 0 and t not in placed]
    ids.sort(key=lambda t: (-uses[t], t))
    return ids[:limit]


def plan_batch(
    conn: sqlite3.Connection,
    vectors_of: Callable,
    *,
    limit: int,
    floor: float = TOPIC_FLOOR,
) -> Plan:
    """Place the `limit` most used topics not placed yet. Writes nothing.

    Each joins, in order: a held canonical with an alias of the same folded words; a
    cluster of this batch with them; the nearest held head or batch cluster head within
    `floor` cosine holding the same numbers; else it heads a new cluster.
    `vectors_of(names)` gives unit vectors (topic_canon.cached_vectors)."""
    import numpy as np

    names = dict(conn.execute("SELECT id, name FROM topics"))
    display = dict(conn.execute("SELECT id, display_name FROM topics"))
    uses = topic_uses(conn)
    plan = Plan(names=names, display=display, uses=uses)
    batch = _unplaced(conn, uses, limit)
    if not batch:
        return plan

    held_fold: dict[str, int] = {}
    for key, canonical in conn.execute(
        "SELECT fold_key, canonical_id FROM topic_aliases WHERE fold_key != ''"
        " GROUP BY fold_key, canonical_id ORDER BY COUNT(*)"
    ):
        held_fold[key] = canonical
    heads = list(conn.execute("SELECT id, head_topic_id FROM canonical_topics ORDER BY id"))
    batch_fold: dict[str, Cluster] = {}
    rest: list[int] = []
    for t in batch:
        key = fold_key(names[t])
        if key and key in held_fold:
            plan.joins.append((t, held_fold[key], "fold", 1.0))
        elif key and key in batch_fold:
            cluster = batch_fold[key]
            cluster.members.append(t)
            cluster.methods[t] = ("fold", 1.0)
        else:
            cluster = Cluster(members=[t], methods={t: ("head", 1.0)})
            if key:
                batch_fold[key] = cluster
            rest.append(t)

    # Embedding: each remaining head, most used first, to the nearest held or new head.
    by_head = {c.members[0]: c for c in batch_fold.values()}
    held_ids = [h for _c, h in heads if h in names]
    held_canon = [c for c, h in heads if h in names]
    vectors = vectors_of([names[t] for t in held_ids + rest])
    held_v, rest_v = vectors[: len(held_ids)], vectors[len(held_ids) :]
    new_heads: list[int] = []  # indexes into rest
    for i, t in enumerate(rest):
        cluster = by_head.get(t) or Cluster(members=[t], methods={t: ("head", 1.0)})
        mine = _numbers(names[t])
        best: tuple[float, str, int] | None = None
        if held_ids:
            sims = held_v @ rest_v[i]
            for j in np.argsort(-sims)[:5]:
                if sims[j] < floor:
                    break
                if _numbers(names[held_ids[j]]) == mine:
                    best = (float(sims[j]), "held", int(j))
                    break
        if new_heads:
            sims = rest_v[new_heads] @ rest_v[i]
            for j in np.argsort(-sims)[:5]:
                if sims[j] < floor:
                    break
                k = new_heads[int(j)]
                if _numbers(names[rest[k]]) == mine:
                    if best is None or float(sims[j]) > best[0]:
                        best = (float(sims[j]), "new", k)
                    break
        if best is None:
            new_heads.append(i)
            plan.clusters.append(cluster)
            by_head[t] = cluster
            continue
        score, where, j = best
        score = round(score, 4)
        if where == "held":
            for m in cluster.members:
                plan.joins.append((m, held_canon[j], "embedding" if m == t else "fold", score))
        else:
            target = by_head[rest[j]]
            for m in cluster.members:
                target.members.append(m)
                target.methods[m] = ("embedding", score) if m == t else ("fold", 1.0)
    return plan


def _needs_model(plan: Plan, cluster: Cluster) -> bool:
    """A cluster of more than one folded form: its name is a choice worth a call."""
    return len({fold_key(plan.names[m]) for m in cluster.members}) > 1


NAME_SYSTEM = (
    "You name clusters of topic tags from a bank executive's mail and chats. Each cluster"
    " lists tags that mean one subject, most used first, with their use counts. Give each a"
    " short canonical name (2-5 words) in the language most of its tags use, as a"
    " person would write it, keeping product names, acronyms and numbers exact. Reply with"
    " ONLY a JSON object mapping each cluster key to its name."
)


def name_clusters(
    conn: sqlite3.Connection,
    plan: Plan,
    *,
    ask: Callable[[str, str], str] | None,
    max_calls: int,
) -> Plan:
    """Name each new cluster: the most used member's name, then, for clusters of several
    folded forms and while `max_calls` lasts, the model's (`ask(system, user)` -> text).
    A reply that is not a JSON object of names keeps the rule names."""
    for c in plan.clusters:
        head = c.members[0]
        c.name, c.display_name, c.named_by = plan.names[head], plan.display[head], "rule"
    wanted = [c for c in plan.clusters if _needs_model(plan, c)]
    for start in range(0, len(wanted), NAME_BATCH):
        if ask is None or plan.llm_calls >= max_calls:
            break
        chunk = wanted[start : start + NAME_BATCH]
        listing = {
            f"c{n}": [
                f"{plan.display[m]} ({plan.uses.get(m, 0)})" for m in c.members[:NAME_MEMBERS]
            ]
            for n, c in enumerate(chunk)
        }
        plan.llm_calls += 1
        try:
            reply = json.loads(
                _json_text(ask(NAME_SYSTEM, json.dumps(listing, ensure_ascii=False)))
            )
        except (ValueError, TypeError):
            continue
        if not isinstance(reply, dict):
            continue
        for n, c in enumerate(chunk):
            given = reply.get(f"c{n}")
            if isinstance(given, str) and normalize_topic(given):
                c.name, c.display_name, c.named_by = normalize_topic(given), given.strip(), "llm"
    return plan


def _json_text(text) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))
    start, end = s.find("{"), s.rfind("}")
    return s[start : end + 1] if start >= 0 and end > start else s


def apply_plan(conn: sqlite3.Connection, plan: Plan) -> dict[str, int]:
    """Write a plan: new canonicals, then every alias (one already placed is kept). Unnamed
    clusters take their head's name. The caller commits. Returns the counts."""
    now = _now()
    counts = plan.counts()
    for c in plan.clusters:
        head = c.members[0]
        canonical = conn.execute(
            "INSERT INTO canonical_topics (name, display_name, named_by, head_topic_id,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (
                c.name or plan.names[head],
                c.display_name or plan.display[head],
                c.named_by,
                head,
                now,
            ),
        ).lastrowid
        assert canonical is not None  # mypy: INSERT always sets it
        for m in c.members:
            method, score = c.methods.get(m, ("fold", 1.0))
            plan.joins.append((m, canonical, method, score))
    for topic, canonical, method, score in plan.joins:
        conn.execute(
            "INSERT OR IGNORE INTO topic_aliases"
            " (topic_id, canonical_id, method, score, fold_key, mapped_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (topic, canonical, method, score, fold_key(plan.names[topic]), now),
        )
    plan.clusters, plan.joins = [], []
    return counts


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows of each registry table, and the topics with uses not placed."""
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    uses = topic_uses(conn)
    placed = {r[0] for r in conn.execute("SELECT topic_id FROM topic_aliases")}
    out["unplaced"] = sum(1 for t, n in uses.items() if n > 0 and t not in placed)
    return out

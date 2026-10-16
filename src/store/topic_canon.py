"""Canonical topics: a topic's family, so a topic read or filter takes its variants too.

On the replica of 2026-10-16 the topics table held 155,902 names, 60.9% used once; folding
plurals and word order merges only about 13k, while 39k rare topics are a frequent topic
plus extra words and 16k are in Greek script. topic_context picked one row by LIKE and
missed every variant. topic_family maps a topic to a head in three ways, none of which
touches the topics table or a link:

  fold          the same words once plurals and order are folded ('credit cards',
                'card credit'): a synonym, both ways
  containment   a rare topic whose words hold a frequent topic's words ('cardless
                payments' under 'payments'): a narrower topic, one way, so a read of
                the head takes it and a read of it does not take the head
  embedding     names whose vectors (gemini-embedding-001 at 256 dimensions) lie within
                a measured similarity floor ('competitor watch', 'competitor
                monitoring', a Greek name and its English one): a synonym, both ways

topic_family_ids gives a topic's family: itself, the topics mapped under it, and, when it
is a synonym of a head (fold or embedding), that head and its family. A store without the
table (before v38) or with no rows has families of one, as before.
"""

import json
import sqlite3

SYNONYM_METHODS = ("fold", "embedding")


def _has_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'topic_family'"
        ).fetchone()
        is not None
    )


def topic_family_ids(
    conn: sqlite3.Connection, topic_id: int, *, narrower: bool = True
) -> list[int]:
    """The topic, the topics mapped under it, and the family of a head it is a synonym
    of; the topic itself first, then the rest by id. Without `narrower`, synonyms alone:
    no topic containment put under any of them."""
    if not _has_table(conn):
        return [topic_id]
    heads = {topic_id}
    heads.update(
        r[0]
        for r in conn.execute(
            "SELECT head_id FROM topic_family WHERE topic_id = ? AND method IN (?, ?)",
            (topic_id, *SYNONYM_METHODS),
        )
    )
    members = set(heads)
    synonyms = "" if narrower else " AND method IN (?, ?)"
    members.update(
        r[0]
        for r in conn.execute(
            "SELECT topic_id FROM topic_family WHERE head_id IN (SELECT value FROM json_each(?))"
            + synonyms,
            (json.dumps(sorted(heads)), *(() if narrower else SYNONYM_METHODS)),
        )
    )
    return [topic_id] + sorted(members - {topic_id})


def family_of_matching_sql(conn: sqlite3.Connection) -> str:
    """A SELECT of topic ids taking one parameter, a LIKE pattern: the topics whose name
    matches it and the family of each (topic_family_ids), for a topic filter that matched
    names by LIKE. Without the table, the matching topics alone, as before v38."""
    if not _has_table(conn):
        return "SELECT id FROM topics WHERE name LIKE ?"
    return (
        "WITH sb_m(id) AS (SELECT id FROM topics WHERE name LIKE ?),"
        " sb_h(id) AS (SELECT id FROM sb_m UNION SELECT f.head_id FROM topic_family f"
        f" JOIN sb_m ON f.topic_id = sb_m.id WHERE f.method IN {SYNONYM_METHODS!r})"
        " SELECT id FROM sb_h UNION SELECT f.topic_id FROM topic_family f"
        " JOIN sb_h ON f.head_id = sb_h.id"
    )


def resolve_topic(conn: sqlite3.Connection, name: str):
    """The topic row a name means, as topic_context has always picked it: the names
    holding the normalized query, an exact name first, then the most used; None for a
    query with no letter or digit, which LIKE '%%' would match to every topic."""
    from src.store.normalizer import normalize_topic

    normalized = normalize_topic(name)
    if not any(ch.isalnum() for ch in normalized):
        return None
    return conn.execute(
        """
        SELECT t.id, t.name, t.display_name FROM topics t
        WHERE t.name LIKE ?
        ORDER BY (t.name = ?) DESC,
                 (SELECT COUNT(*) FROM email_topics et WHERE et.topic_id = t.id) DESC, t.id
        LIMIT 1
        """,
        (f"%{normalized}%", normalized),
    ).fetchone()


def family_ids_for_name(conn: sqlite3.Connection, name: str) -> list[int]:
    """The family of the topic `name` resolves to; [] when it resolves to none."""
    row = resolve_topic(conn, name)
    return topic_family_ids(conn, row[0]) if row is not None else []


def family_names(conn: sqlite3.Connection, ids: list[int], limit: int = 20) -> list[str]:
    """Display names of a family's topics, in the order given, at most `limit`."""
    if not ids:
        return []
    rows = dict(
        conn.execute(
            "SELECT id, display_name FROM topics WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        ).fetchall()
    )
    return [rows[i] for i in ids if i in rows][:limit]


# --- building the families ------------------------------------------------------------------

# A topic used this often or more can head a family; one used this rarely or less is mapped
# under a head whose words its own hold (containment).
FREQUENT_USES = 5
RARE_USES = 2
# At most this many heads a rare topic is mapped under (the most specific ones).
MAX_HEADS = 3
# Embedding: gemini-embedding-001 at this many dimensions; a name within EMBED_FLOOR cosine
# of a more used name, and holding the same numbers, is its synonym. Measured on the
# replica of 2026-10-16, 3,000 names, each to its nearest more-used name, 12 pairs a band
# judged by hand: at 0.94 and over 12 of 12 were one topic, 0.92-0.94 9 of 12 (the three
# misses: two other quarters, which the number rule refuses, and one narrower topic),
# 0.90-0.92 9 of 12, 0.88-0.90 7 of 12. 2% of names had a neighbour at 0.92 or more.
EMBED_DIM = 256
EMBED_FLOOR = 0.92
EMBED_BATCH = 100
# Rows of the similarity matrix computed at once: 512 x 70k float32 is about 140 MB.
SIM_BLOCK = 512
VECTORS_FILE = "topic_vectors.npz"

# Words that hold no topic of their own.
STOPWORDS = frozenset(
    "a an and at by de for from in into of on or per the to vs with και του της των το τα η ο"
    " οι σε για με από στο στη στην στον στα".split()
)


def _words(name: str) -> frozenset[str]:
    """A topic's content words, folded, plurals as singulars ('cards' is 'card')."""
    import re
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", name or "")
    bare = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    out = set()
    for word in re.split(r"[\W_]+", bare.replace("ς", "σ")):
        if not word or word in STOPWORDS:
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return frozenset(out)


def topic_uses(conn: sqlite3.Connection) -> dict[int, int]:
    """How many emails and conversations each topic tags."""
    uses: dict[int, int] = {}
    for table in ("email_topics", "conversation_topics"):
        try:
            for tid, n in conn.execute(f"SELECT topic_id, COUNT(*) FROM {table} GROUP BY topic_id"):
                uses[tid] = uses.get(tid, 0) + n
        except sqlite3.OperationalError:
            continue
    return uses


def _mapped(conn: sqlite3.Connection) -> tuple[set[int], set[int]]:
    """The topics mapped under a head already, and the heads."""
    if not _has_table(conn):
        return set(), set()
    members = {r[0] for r in conn.execute("SELECT DISTINCT topic_id FROM topic_family")}
    heads = {r[0] for r in conn.execute("SELECT DISTINCT head_id FROM topic_family")}
    return members, heads


def plan_fold_and_containment(conn: sqlite3.Connection) -> list[tuple[int, int, str, float]]:
    """The rows (topic, head, method, score) folding and containment give the topics not
    mapped yet. A head is never itself a member."""
    names = dict(conn.execute("SELECT id, name FROM topics"))
    uses = topic_uses(conn)
    member, heads_now = _mapped(conn)
    words = {t: _words(n) for t, n in names.items()}
    rows: list[tuple[int, int, str, float]] = []

    def rank(t: int) -> tuple:
        return (-uses.get(t, 0), t)

    # fold: the same content words, under the most used of them
    groups: dict[frozenset, list[int]] = {}
    for t, w in words.items():
        if w:
            groups.setdefault(w, []).append(t)
    for group in groups.values():
        if len(group) < 2:
            continue
        head = min([t for t in group if t in heads_now] or group, key=rank)
        for t in group:
            if t != head and t not in member and t not in heads_now:
                rows.append((t, head, "fold", 1.0))
                member.add(t)
    # containment: a rare topic under the most specific frequent topics its words hold
    frequent = [
        t
        for t, w in words.items()
        if uses.get(t, 0) >= FREQUENT_USES and t not in member and any(not x.isdigit() for x in w)
    ]
    by_word: dict[str, list[int]] = {}
    for t in frequent:
        for word in words[t]:
            by_word.setdefault(word, []).append(t)
    for t, w in words.items():
        if t in member or t in heads_now or uses.get(t, 0) > RARE_USES or len(w) < 2:
            continue
        candidates = {h for x in w for h in by_word.get(x, ()) if h != t and words[h] < w}
        maximal = [h for h in candidates if not any(words[h] < words[o] for o in candidates)]
        for h in sorted(maximal, key=lambda h: (-len(words[h]), rank(h)))[:MAX_HEADS]:
            rows.append((t, h, "containment", round(len(words[h]) / len(w), 3)))
            member.add(t)
    return rows


def write_rows(conn: sqlite3.Connection, rows) -> int:
    """Add family rows; one already there is kept. Returns the rows added. The caller
    commits."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat(timespec="seconds")
    added = 0
    for topic, head, method, score in rows:
        added += conn.execute(
            "INSERT OR IGNORE INTO topic_family (topic_id, head_id, method, score, mapped_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (topic, head, method, score, now),
        ).rowcount
    return added


def embed_names(names: list[str], client=None):
    """Unit vectors of `names` at EMBED_DIM dimensions, through the embedding client of
    src/store/embeddings.py (the Gemini API with a key, else Vertex), in batches."""
    import time

    import numpy as np
    from google.genai import types

    from src.store.embeddings import EMBEDDING_MODEL, _get_client

    client = client or _get_client()
    config = types.EmbedContentConfig(output_dimensionality=EMBED_DIM)
    out = np.empty((len(names), EMBED_DIM), dtype=np.float32)
    for start in range(0, len(names), EMBED_BATCH):
        batch = names[start : start + EMBED_BATCH]
        for attempt in range(5):
            try:
                result = client.models.embed_content(
                    model=EMBEDDING_MODEL, contents=batch, config=config
                )
                break
            except Exception as e:
                if attempt == 4 or not ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)):
                    raise
                time.sleep(min(2**attempt * 5, 60))
        vectors = [e.values for e in result.embeddings]
        if len(vectors) != len(batch):
            raise RuntimeError(f"{len(vectors)} vectors for {len(batch)} names")
        out[start : start + len(batch)] = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.maximum(norms, 1e-12)


def plan_embedding(
    conn: sqlite3.Connection, vectors_of, floor: float = EMBED_FLOOR
) -> list[tuple[int, int, str, float]]:
    """Synonym rows by embedding among the topics folding and containment left unmapped:
    most used first, each topic joins the most similar more-used root within `floor` of
    it, else is a root itself. `vectors_of(names)` gives their unit vectors."""
    import numpy as np

    names = dict(conn.execute("SELECT id, name FROM topics"))
    uses = topic_uses(conn)
    member, _heads = _mapped(conn)
    ids = sorted(
        (t for t in names if t not in member and uses.get(t, 0) > 0),
        key=lambda t: (-uses.get(t, 0), t),
    )
    if len(ids) < 2:
        return []
    vectors = vectors_of([names[t] for t in ids])
    # Names holding other numbers are other periods ('q1 2026', 'q2 2026' at 0.94).
    numbers = [frozenset(w for w in _words(names[t]) if any(c.isdigit() for c in w)) for t in ids]
    root = list(range(len(ids)))
    rows = []
    for start in range(1, len(ids), SIM_BLOCK):
        block = vectors[start : start + SIM_BLOCK]
        sims = block @ vectors[: start + len(block)].T
        for offset in range(len(block)):
            i = start + offset
            row = sims[offset, :i]
            for j in np.argsort(-row)[:5]:
                if row[j] < floor:
                    break
                r = root[j]
                score = float(vectors[i] @ vectors[r])
                if score >= floor and numbers[i] == numbers[r]:
                    root[i] = r
                    rows.append((ids[i], ids[r], "embedding", round(score, 4)))
                    break
    return rows


def cached_vectors(path):
    """vectors_of with a file cache keyed by name: a name embedded before is not sent
    again."""
    import numpy as np

    def vectors_of(names):
        cache: dict[str, object] = {}
        if path.exists():
            data = np.load(path, allow_pickle=False)
            cache = dict(zip(data["names"].tolist(), data["vectors"], strict=True))
        missing = [n for n in dict.fromkeys(names) if n not in cache]
        if missing:
            cache.update(zip(missing, embed_names(missing), strict=True))
            keys = list(cache)
            np.savez(path, names=np.array(keys), vectors=np.stack([cache[k] for k in keys]))
        return np.stack([cache[n] for n in names])

    return vectors_of


def cmd_topic_canon(args) -> int:
    """`brain maint topic-canon`: a dry run unless --apply; --embed adds the synonyms."""
    import sys
    from collections import Counter
    from pathlib import Path

    from src.config import DATA_ROOT
    from src.store.schema import get_connection, run_migrations

    if args.embed and not args.apply:
        print("--embed needs --apply: its rows depend on the rows before it", file=sys.stderr)
        return 2
    conn = get_connection(str(args.db))
    run_migrations(conn)
    try:
        rows = plan_fold_and_containment(conn)
        counts = Counter(method for _t, _h, method, _s in rows)
        if args.apply:
            counts["added"] = write_rows(conn, rows)
            conn.commit()
        if args.embed:
            synonyms = plan_embedding(
                conn, cached_vectors(Path(DATA_ROOT) / VECTORS_FILE), args.floor
            )
            counts["embedding"] = len(synonyms)
            counts["added"] += write_rows(conn, synonyms)
            conn.commit()
        names = dict(conn.execute("SELECT id, name FROM topics"))
        print(
            f"topic families: {dict(counts)}" + ("" if args.apply else " (dry run; --apply writes)")
        )
        for topic, head, method, score in rows[: args.show]:
            print(f"  {method:11} {score:5}  {names.get(topic)!r} -> {names.get(head)!r}")
    finally:
        conn.close()
    return 0


def add_maint_parser(maint_subparsers) -> None:
    """`brain maint topic-canon`."""
    import argparse

    parser = maint_subparsers.add_parser(
        "topic-canon",
        help="Map topics to families: folding, containment, embedding (no topic changes)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="Write the rows (default: count)")
    parser.add_argument(
        "--embed", action="store_true", help="Also embed the rest and add synonyms (with --apply)"
    )
    parser.add_argument("--floor", type=float, default=EMBED_FLOOR, help="Synonym cosine floor")
    parser.add_argument("--show", type=int, default=10, help="Rows to print")
    parser.set_defaults(func=cmd_topic_canon)

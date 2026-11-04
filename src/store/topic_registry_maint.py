"""`brain maint topic-registry`: one batch of the topic registry (src/store/topic_registry.py).

Kept apart from the registry so the MCP server, which reads the registry, does not load
the batch's model and pipeline modules.
"""

from src.store import topic_registry
from src.store.topic_registry import (
    NAME_MAX_TOKENS,
    TOPIC_FLOOR,
    VECTORS_FILE,
    apply_plan,
    cached_topic_vectors,
    counts,
    enabled,
    name_clusters,
    plan_batch,
)


def cmd_topic_registry(args) -> int:
    """`brain maint topic-registry`: one batch, a dry run unless --apply."""
    import sys
    from pathlib import Path

    from src.config import DATA_ROOT
    from src.store.schema import ensure_schema, get_connection

    conn = get_connection(str(args.db))
    ensure_schema(conn)
    try:
        if not enabled(conn):
            print(
                "the topic registry is a gated step: run `brain maint migrate --apply-gated"
                " --backup PATH` first",
                file=sys.stderr,
            )
            return 2
        vectors_of = cached_topic_vectors(Path(DATA_ROOT) / VECTORS_FILE)
        plan = plan_batch(conn, vectors_of, limit=args.limit, floor=args.floor)
        ask = None
        if args.llm_calls > 0:
            from src.pipeline.topic_pages import ask as page_ask

            def ask(system: str, user: str) -> str:
                return page_ask(system, user, max_tokens=NAME_MAX_TOKENS).text

        plan = name_clusters(conn, plan, ask=ask, max_calls=args.llm_calls)
        shown = [
            (c.display_name, [plan.display[m] for m in c.members[:6]])
            for c in plan.clusters
            if len(c.members) > 1
        ][: args.show]
        if args.apply:
            result = apply_plan(conn, plan)
            conn.commit()
        else:
            result = plan.counts()
        print(f"topic registry: {result}" + ("" if args.apply else " (dry run; --apply writes)"))
        for name, members in shown:
            print(f"  {name!r} <- {members}")
        print(f"after: {counts(conn)}")
    finally:
        conn.close()
    return 0


def add_maint_parser(maint_subparsers) -> None:
    """`brain maint topic-registry`."""
    import argparse

    parser = maint_subparsers.add_parser(
        "topic-registry",
        help="Cluster unplaced topics into canonical topics, one batch (needs the gated step)",
        description=topic_registry.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="Write the batch (default: count)")
    parser.add_argument("--limit", type=int, default=5000, help="Topics placed this run")
    parser.add_argument(
        "--llm-calls", type=int, default=0, help="Model calls naming clusters (0: rule names)"
    )
    parser.add_argument("--floor", type=float, default=TOPIC_FLOOR, help="Embedding cosine floor")
    parser.add_argument("--show", type=int, default=15, help="Clusters to print")
    parser.set_defaults(func=cmd_topic_registry)

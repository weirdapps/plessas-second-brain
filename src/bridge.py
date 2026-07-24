"""Bridge API for external plugin integration (e.g., email-handler).

Provides a simple CLI interface for other plugins to query second-brain context.
Outputs JSON to stdout for easy consumption.

Usage:
    python -m src.bridge person-context "alice@example.com"
    python -m src.bridge person-context "Alice Smith" --days 30
    python -m src.bridge topic-context "cards migration"
    python -m src.bridge sender-brief "alice@example.com"
"""

import json
import sys

from src.config import DEFAULT_DB
from src.store.schema import get_connection


def sender_brief(conn, name_or_email: str, days: int = 365) -> dict:
    """Quick sender briefing for email-handler integration.

    Returns a compact summary suitable for inline display in inbox briefings
    and mail review. Lighter than full person_context.

    Args:
        conn: Database connection
        name_or_email: Sender name or email
        days: Lookback period

    Returns:
        Dict with: known (bool), name, email, role, email_count,
        top_topics, recent_decisions, open_actions_count, last_contact
    """
    from src.store.context import get_person_context

    ctx = get_person_context(conn, name_or_email, days=days)

    if not ctx["person"]:
        return {"known": False, "query": name_or_email}

    person = ctx["person"]
    return {
        "known": True,
        "name": person.get("name", ""),
        "email": person.get("email", ""),
        "role": person.get("role", ""),
        "email_count": ctx["email_count"],
        "top_topics": [t["topic"] for t in ctx["topics"][:5]],
        "recent_decisions": len(ctx["decisions"]),
        "open_actions_count": len(ctx["open_actions"]),
        "last_contact": ctx["communication_pattern"].get("last_email_date", ""),
        "sentiment": ctx["sentiment_distribution"],
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: python -m src.bridge <command> <query> [--days N]", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    query = sys.argv[2]
    days = 365

    # Parse --days flag
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])

    db_path = str(DEFAULT_DB)

    try:
        conn = get_connection(db_path)
    except FileNotFoundError:
        json.dump(
            {"error": f"Database not found: {db_path}", "known": False},
            sys.stdout,
            indent=2,
        )
        print()
        sys.exit(1)

    try:
        if command == "person-context":
            from src.store.context import get_person_context

            result = get_person_context(conn, query, days=days)
        elif command == "topic-context":
            from src.store.context import get_topic_context

            result = get_topic_context(conn, query, days=days)
        elif command == "sender-brief":
            result = sender_brief(conn, query, days=days)
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            sys.exit(1)

        json.dump(result, sys.stdout, indent=2, default=str)
        print()
    finally:
        conn.close()


if __name__ == "__main__":
    main()

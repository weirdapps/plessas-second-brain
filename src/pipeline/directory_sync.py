"""`brain directory-sync`: what the organisation's directory says of the owner's colleagues.

The people at the owner's own mail domains (BRAIN_USER_EMAIL_PATTERN, or --domain) that
are due (src/store/directory.py: never asked, or past their refresh age) are looked up
through `teams-cli lookup-users`, which asks Microsoft Graph with the Teams session, in
chunks of CHUNK addresses. Each chunk is stored as it comes back, so a run stopped by an
expired sign-in keeps what it stored and exits 4 for the re-authentication the wrapper
watches for. No model is called.
"""

import subprocess
import sys
from pathlib import Path

from src.config import USER_EMAIL_PATTERN
from src.export.teams_cli import TeamsCliAuthRequired, TeamsCliError, run_teams_cli
from src.pipeline import EXIT_REAUTH
from src.store import directory
from src.store.schema import get_connection, run_migrations

# Addresses per teams-cli call; the CLI batches them twenty to a Graph request.
CHUNK = 200
CALL_TIMEOUT_S = 180


def _report(totals: dict) -> None:
    print(
        f"Directory: {totals['ok']:,} found, {totals['not_found']:,} not in the directory, "
        f"{totals['error']:,} failed (asked again next run)"
    )


def run(args) -> int:
    """Look up the due colleagues and store the answers; 0, 1 on failure, 2 on no
    domain, EXIT_REAUTH on an expired sign-in."""
    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first.", file=sys.stderr)
        return 1
    domains = sorted(
        {d.strip().lower().lstrip("@") for d in (args.domain or []) if d.strip()}
    ) or directory.org_domains(USER_EMAIL_PATTERN)
    if not domains:
        print(
            "Error: no domain to look up. Set BRAIN_USER_EMAIL_PATTERN to the owner's "
            "address, or pass --domain.",
            file=sys.stderr,
        )
        return 2

    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        due = directory.due_addresses(conn, domains, limit=args.limit or 0)
        print(f"Directory: {len(due):,} address(es) due at {', '.join(domains)}")
        if args.dry_run:
            print("DRY RUN: nothing looked up")
            return 0
        totals = {"ok": 0, "not_found": 0, "error": 0}
        for start in range(0, len(due), CHUNK):
            chunk = due[start : start + CHUNK]
            try:
                answer = run_teams_cli(["lookup-users", *chunk], timeout_sec=CALL_TIMEOUT_S)
            except TeamsCliAuthRequired as e:
                _report(totals)
                print(
                    f"Directory: the Teams sign-in expired ({e.stderr}); run 'teams-cli login'",
                    file=sys.stderr,
                )
                return EXIT_REAUTH
            except (TeamsCliError, subprocess.TimeoutExpired) as e:
                _report(totals)
                print(f"Directory: teams-cli lookup-users failed: {e}", file=sys.stderr)
                return 1
            counts = directory.store_lookups(conn, (answer or {}).get("users", []))
            for key in totals:
                totals[key] += counts[key]
        _report(totals)
        return 0
    finally:
        conn.close()

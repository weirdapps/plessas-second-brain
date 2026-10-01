"""Read-only SQL over brain.db, for the sql_query and sql_schema MCP tools.

Every other tool returns summaries and snippets. This is how an agent counts
something the curated tools cannot express, or reads a full body. It must never
write: the connection opens read-only with query_only on, and an authorizer
allows nothing but reading. One statement per call, a time budget, and caps on
rows, cells and the whole answer keep a careless query from flooding the
caller's context.
"""

import sqlite3
import time
from pathlib import Path

from src.config import DEFAULT_DB, REPLICA_STAMP
from src.store.greek import register_sql_functions

MAX_ROWS = 200
CELL_CHARS = 4000
TOTAL_CHARS = 100_000
BUDGET_SECONDS = 10.0
_PROGRESS_STEPS = 10_000

_READ_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)
# Pragmas that only describe the schema. FTS5 reads data_version on every MATCH,
# so denying it would break full-text search through this tool.
_READ_ONLY_PRAGMAS = frozenset(
    {
        "data_version",
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
    }
)
_FTS5_SHADOW_SUFFIXES = ("data", "idx", "content", "docsize", "config")


def _authorize(
    action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None
) -> int:
    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in _READ_ONLY_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect_read_only(db_path: Path | None = None) -> sqlite3.Connection:
    """A read-only connection with the store's SQL functions and query_only on."""
    path = Path(db_path or DEFAULT_DB)
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    if REPLICA_STAMP.exists():
        # A pulled replica: nothing writes here, and a plain read-only open
        # fails with SQLITE_CANTOPEN (14). The producer must never take this
        # branch: immutable=1 skips locking on a database that is being written.
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    register_sql_functions(conn)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _arm_budget(conn: sqlite3.Connection) -> None:
    deadline = time.monotonic() + BUDGET_SECONDS
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_STEPS)


def _cell(value: object) -> object:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str) and len(value) > CELL_CHARS:
        return f"{value[:CELL_CHARS]}… [cut, {len(value)} chars]"
    return value


def run_query(sql: str, limit: int = MAX_ROWS, db_path: Path | None = None) -> dict:
    """Run one read-only statement; rows and cells capped, writes refused."""
    limit = max(1, min(int(limit), MAX_ROWS))
    try:
        conn = connect_read_only(db_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    _arm_budget(conn)
    conn.set_authorizer(_authorize)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or ()]
        fetched = cur.fetchmany(limit + 1)
    except sqlite3.OperationalError as exc:
        if str(exc) == "interrupted":
            return {
                "error": f"query exceeded the {BUDGET_SECONDS:.0f} s budget; narrow it or add a LIMIT"
            }
        return {"error": f"SQLite: {exc}"}
    except (sqlite3.DatabaseError, sqlite3.Warning) as exc:
        return {"error": f"refused: {exc}. One read-only SELECT per call."}
    finally:
        conn.close()

    truncated = len(fetched) > limit
    rows: list[list[object]] = []
    total = 0
    for raw in fetched[:limit]:
        row = [_cell(v) for v in tuple(raw)]
        size = sum(len(str(v)) for v in row)
        if rows and total + size > TOTAL_CHARS:
            truncated = True
            break
        rows.append(row)
        total += size
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def describe(table: str | None = None, db_path: Path | None = None) -> dict:
    """Every table and view with its row count, or one table's columns and indexes."""
    try:
        conn = connect_read_only(db_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    try:
        if table is not None:
            found = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
                (table,),
            ).fetchone()
            if found is None:
                return {
                    "error": f"no table or view named {table!r}; call sql_schema() for the list"
                }
            columns = [
                dict(r)
                for r in conn.execute(
                    'SELECT name, type, "notnull" AS not_null, pk FROM pragma_table_info(?)',
                    (table,),
                )
            ]
            indexes = [
                dict(r)
                for r in conn.execute('SELECT name, "unique" FROM pragma_index_list(?)', (table,))
            ]
            return {"table": table, "columns": columns, "indexes": indexes}

        entries = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        virtual = [
            e["name"] for e in entries if (e["sql"] or "").startswith("CREATE VIRTUAL TABLE")
        ]
        shadows = {f"{v}_{suffix}" for v in virtual for suffix in _FTS5_SHADOW_SUFFIXES}
        _arm_budget(conn)
        tables: list[dict] = []
        for entry in entries:
            name = entry["name"]
            if name in shadows:
                continue
            quoted = name.replace('"', '""')
            try:
                rows: int | None = conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            except sqlite3.OperationalError:
                rows = None  # budget spent, or a view that cannot be counted
            tables.append({"table": name, "rows": rows})
        return {"tables": tables}
    finally:
        conn.close()

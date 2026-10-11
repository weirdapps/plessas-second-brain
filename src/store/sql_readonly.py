"""Read-only SQL over brain.db, for the sql_query and sql_schema MCP tools.

Every other tool returns summaries and snippets. This is how an agent counts
something the curated tools cannot express, or reads a full body. It must never
write: the connection opens read-only with query_only on, and an authorizer
allows nothing but reading. One statement per call, a time budget, and caps on
rows, cells and the whole answer keep a careless query from flooding the
caller's context.

connect_read_only is also the store's one read-only open: every MCP tool that
reads, and src.store.schema.get_connection on a replica, go through it.
"""

import sqlite3
import time
from pathlib import Path

from src.config import DEFAULT_DB, is_pulled_copy
from src.store.greek import register_sql_functions

MAX_ROWS = 200
CELL_CHARS = 4000
TOTAL_CHARS = 100_000
BUDGET_SECONDS = 10.0
# The caps above only shorten values already in memory, and a row arrives with
# all its values at once: one row of 40 values of 8 MB peaked at 949 MB (about
# 3x per value). So SQLite builds and reads no value over MAX_VALUE_BYTES (the
# largest stored value is 3.6 MB) and no result wider than MAX_COLUMNS, which
# must stay above the widest table (calendar_events, 23 columns counting
# generated ones) or no query can run.
MAX_VALUE_BYTES = 8 * 1024 * 1024
MAX_COLUMNS = 32
_PROGRESS_STEPS = 10_000
# As src.store.schema.get_connection: long enough to outlast any one write
# transaction. Only a locking open waits; an immutable one takes no locks.
_BUSY_TIMEOUT_SECONDS = 60.0
# Room for the cut marker, "… [cut, N chars]", with N up to eight digits.
_MARKER_CHARS = len("… [cut, 99999999 chars]")

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
# Virtual tables that read the file page by page, free pages included: sqlite_dbpage
# returns each page's bytes where a build compiles it in, dbstat describes every page,
# and the recovery extension's sqlite_dbdata and sqlite_dbptr decode records from
# them. Free pages can hold deleted text (secure_delete zeroes only what is freed
# while it is on), so these are denied by name, whatever this build has.
_RAW_PAGE_TABLES = frozenset({"sqlite_dbpage", "dbstat", "sqlite_dbdata", "sqlite_dbptr"})


def _authorize(
    action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None
) -> int:
    if action == sqlite3.SQLITE_READ and (arg1 or "").lower() in _RAW_PAGE_TABLES:
        return sqlite3.SQLITE_DENY
    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _READ_ONLY_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect_read_only(
    db_path: Path | str | None = None, *, immutable: bool = False
) -> sqlite3.Connection:
    """A connection that cannot write, with the store's SQL functions and query_only on.

    It opens with SQLite's locking (mode=ro), so it reads what a writer committed,
    the WAL included, and never a page mid-write. immutable=1 (no locks, no -wal or
    -shm, any WAL ignored) is taken only where the store is a pulled copy, which
    nothing writes (src.config.is_pulled_copy): when the caller asks for it, or when
    the locking open fails, as it does on a WAL file copied without its -shm into a
    directory this process cannot create one in. Anywhere else immutable is never
    used: the producer serves while its timers write the same file.
    """
    return _connect(db_path, immutable=immutable, limits=())


def _connect(
    db_path: Path | str | None, *, immutable: bool, limits: tuple[tuple[int, int], ...]
) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB)
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")
    pulled = is_pulled_copy()
    if immutable and pulled:
        return _open(path, immutable=True, limits=limits)
    try:
        return _open(path, immutable=False, limits=limits)
    except sqlite3.OperationalError as exc:
        if not (pulled and _cannot_open_read_only(exc)):
            raise
    return _open(path, immutable=True, limits=limits)


def _cannot_open_read_only(exc: sqlite3.OperationalError) -> bool:
    """Whether a locking read-only open failed for want of its -wal or -shm
    (SQLITE_CANTOPEN or SQLITE_READONLY, extended codes included), which an immutable
    open does not need, and not because the file is damaged or no database."""
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    return code in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY)


def _open(
    path: Path, *, immutable: bool, limits: tuple[tuple[int, int], ...]
) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        for category, value in limits:
            conn.setlimit(category, value)
        conn.row_factory = sqlite3.Row
        register_sql_functions(conn)
        # The first read loads the schema, and a locking open reaches for its -wal and
        # -shm with it: a store that cannot be opened this way fails here, where the
        # caller can still take the immutable open, not at its first query.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        # SQLite sizes a sort's in-memory runs from cache_size. A sort that carries
        # long texts peaked at 1.06 GB on a real store with the default 2 MB, and
        # at 0.31 GB with 16 MB. temp_store=FILE keeps sort data on disk even where
        # a build defaults temporary storage to memory.
        conn.execute("PRAGMA cache_size = -16384")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        # A file that is not a database, a schema too wide to read, or a store this
        # process cannot open read-only with locks.
        conn.close()
        raise
    return conn


def _connect_for_sql(db_path: Path | None) -> sqlite3.Connection:
    """The read-only open with the caps an arbitrary query needs, set before the schema
    is read. Immutable on a pulled copy, as sql_query has always opened one there."""
    limits = (
        (sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES),
        (sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS),
    )
    return _connect(db_path, immutable=True, limits=limits)


def _arm_budget(conn: sqlite3.Connection) -> None:
    deadline = time.monotonic() + BUDGET_SECONDS
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_STEPS)


def _cell(value: object, width: int = CELL_CHARS) -> object:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str) and len(value) > width:
        return f"{value[:width]}… [cut, {len(value)} chars]"
    return value


def run_query(sql: str, limit: int = MAX_ROWS, db_path: Path | None = None) -> dict:
    """Run one read-only statement; rows and cells capped, writes refused."""
    limit = max(1, min(int(limit), MAX_ROWS))
    try:
        conn = _connect_for_sql(db_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        return {"error": str(exc)}
    _arm_budget(conn)
    conn.set_authorizer(_authorize)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or ()]
        rows: list[list[object]] = []
        total = 0
        truncated = False
        # One row at a time, cut as it arrives, so no more than one uncut row is
        # ever held and nothing past the caps is read. The cursor itself steps one
        # row ahead, so SQLite evaluates at most one row more than is read here.
        for raw in cur:
            if len(rows) == limit:
                truncated = True
                break
            row = [_cell(v) for v in tuple(raw)]
            if any(
                (isinstance(v, str) and len(v) > CELL_CHARS) or (isinstance(v, bytes) and v)
                for v in raw
            ):
                # A cut cell left text out, as a dropped row does, and so did a BLOB
                # given as its size (an empty one left nothing out).
                truncated = True
            size = sum(len(str(v)) for v in row)
            if not rows and size > TOTAL_CHARS:
                # One row wider than the whole answer: every cell gets an equal
                # share, cut marker included, so the cap holds for every answer. A
                # share is at least TOTAL_CHARS // MAX_COLUMNS characters.
                width = max(0, TOTAL_CHARS // len(row) - _MARKER_CHARS)
                row = [_cell(v, width) for v in tuple(raw)]
                size = sum(len(str(v)) for v in row)
                truncated = True
            del raw
            if rows and total + size > TOTAL_CHARS:
                truncated = True
                break
            rows.append(row)
            total += size
    except sqlite3.OperationalError as exc:
        # Errors the sqlite3 module raises itself (a column that is not UTF-8)
        # carry no sqlite_errorcode.
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            return {
                "error": f"query exceeded the {BUDGET_SECONDS:.0f} s budget; narrow it or add a LIMIT"
            }
        if "too many columns" in str(exc):
            return {
                "error": f"the result has more than {MAX_COLUMNS} columns; "
                "name only the columns you need instead of SELECT *"
            }
        return {"error": f"SQLite: {exc}"}
    except sqlite3.DataError:  # SQLITE_TOOBIG: a value over MAX_VALUE_BYTES
        return {
            "error": f"a value is too big (over {MAX_VALUE_BYTES:,} bytes); select a slice "
            f"with substr(column, start, {CELL_CHARS}), or combine fewer rows"
        }
    except (sqlite3.DatabaseError, sqlite3.Warning) as exc:
        return {"error": f"refused: {str(exc).rstrip('.')}. One read-only SELECT per call."}
    finally:
        conn.close()
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def describe(table: str | None = None, db_path: Path | None = None) -> dict:
    """Every table and view with its row count, or one table's columns and indexes.

    Full-text (virtual) tables are listed with rows None, not counted: counting
    one scans its whole content table, which took over 30 s on a real store.
    """
    try:
        conn = _connect_for_sql(db_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
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
            if name in virtual:
                tables.append({"table": name, "rows": None, "virtual": True})
                continue
            quoted = name.replace('"', '""')
            try:
                rows: int | None = conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            except sqlite3.OperationalError:
                rows = None  # budget spent, or a view that cannot be counted
            tables.append({"table": name, "rows": rows, "virtual": False})
        return {"tables": tables}
    finally:
        conn.close()

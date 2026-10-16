"""The attachment and sync stamps in UTC, and the two forms the store holds until converted.

Until 2026-10-16 the attachment pipeline wrote attachment_content.extracted_at and
llm_extracted_at, and sync wrote sync_metadata's last_sync_date, in the producer's local time
with no offset (datetime.now().isoformat()), as reextract wrote reextract_partial_since and
refetch-garbled the times in its records. The store's other stamps are UTC. Read against those,
or by SQLite's julianday(), which takes a stamp with no offset for UTC, a lag came out up to three
hours wrong and could read negative (audit 2026-10-16).

They are written in UTC with an explicit offset now (utc_now). `brain maint utc-stamps` converts
the stored ones (convert). Until it has run a column holds both forms, so every reader that
compares them compares instants: instant() in Python and instant_sql() in SQL, both reading a
stamp with no offset as local time, the zone that wrote it: the producer's, Europe/Athens, and a
Mac replica's, the same.

The conversion keeps each attachment_content stamp as wide as it was ("2026-10-16T12:16:09.841728"
becomes "2026-10-16T09:16:09.84172Z", the last digit of the microseconds dropped for the "Z"). Both
stamps sit behind extracted_text, 3 GB on the producer: SQLite overwrites a row of unchanged size
in place and writes only the pages whose bytes change, while a stamp of another width would have
the row, its whole text, written again. Updating a stamp still reads the row's text, to rebuild
the row and the sweep index's length(extracted_text), so the conversion reads every stored text
once and commits every CHUNK rows: minutes on the producer, with the timers running.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime

# Rows converted per transaction. Each holds the write lock for a few seconds at most.
CHUNK = 500

# A stamp that names its zone: "Z", or an offset after its time.
_OFFSET = re.compile(r"(?:Z|[+-]\d{2}(?::?\d{2})?)$", re.IGNORECASE)

# The attachment_content stamps, each with the index that holds it beside the row id, so finding
# the ones in local time reads no stored text (schema v32, src/store/schema.py).
COLUMNS = (
    ("extracted_at", "idx_attachment_content_sweep"),
    ("llm_extracted_at", "idx_attachment_content_llm_extracted"),
)
# The sync_metadata rows written in local time: last_sync_date (src/pipeline/sync.py) and
# reextract_partial_since (src/extract/reextract.py, src/store/file_sweep.py PARTIAL_SINCE_KEY).
SYNC_KEYS = ("last_sync_date", "reextract_partial_since")
# refetch-garbled's records (src/store/file_sweep.py REFETCH_KEY) and their time fields.
REFETCH_PREFIX = "refetch_garbled:"
REFETCH_FIELDS = ("fetched_at", "last_attempt_at", "gone_at")


def utc_now() -> str:
    """Now, as the stamps are written: UTC with its offset, "2026-10-16T09:16:09.841728+00:00"."""
    return datetime.now(UTC).isoformat()


def has_offset(stamp: str) -> bool:
    """Whether a stamp names its zone."""
    return bool(_OFFSET.search(stamp[10:]))


def instant(stamp) -> datetime | None:
    """A stored stamp as an aware datetime, in UTC; a stamp with no offset is local time. None for
    none, or for one that does not parse."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return when.astimezone(UTC)


def instant_sql(expr: str) -> str:
    """SQL for the Julian day of a stamp in either form, NULL for none: julianday() of a stamp
    that names its zone, and of one that does not read as local time (the 'utc' modifier)."""
    return (
        f"(CASE WHEN {expr} GLOB '*[Zz+]*' OR substr({expr}, 11) GLOB '*-[0-9][0-9]:[0-9][0-9]'"
        f" THEN julianday({expr}) ELSE julianday({expr}, 'utc') END)"
    )


def iso_sql(julian: str) -> str:
    """SQL that writes a Julian day as a UTC stamp, "2026-10-16T09:16:09.841Z"."""
    return f"strftime('%Y-%m-%dT%H:%M:%fZ', {julian})"


def to_utc(stamp: str) -> str | None:
    """A stamp in local time as UTC, as wide as it was when it has a fraction of a second; None
    for one that names its zone already or does not parse."""
    if not stamp or has_offset(stamp):
        return None
    when = instant(stamp)
    if when is None:
        return None
    body = when.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = stamp[19:]
    if fraction.startswith(".") and len(fraction) >= 3:
        return f"{body}.{when.microsecond:06d}"[: len(body) + len(fraction) - 1] + "Z"
    return f"{body}Z"


def _utc_iso(stamp: object) -> str | None:
    """A small stamp's UTC form, as utc_now() writes one; None when it needs none."""
    if not isinstance(stamp, str) or has_offset(stamp):
        return None
    when = instant(stamp)
    return None if when is None else when.isoformat()


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA index_list(attachment_content)")}


def _local_rows(conn: sqlite3.Connection, column: str) -> dict[int, str]:
    """{content id: stamp} of the rows whose `column` is in local time, read from the index that
    holds it when the store has it."""
    index = dict(COLUMNS)[column]
    hint = f" INDEXED BY {index}" if index in _indexes(conn) else ""
    return {
        ac_id: stamp
        for ac_id, stamp in conn.execute(
            f"SELECT id, {column} FROM attachment_content{hint} WHERE {column} IS NOT NULL"
        )
        if not has_offset(stamp)
    }


def convert(conn: sqlite3.Connection, *, apply: bool) -> dict:
    """Find, and with `apply` convert, the stamps stored in local time. A row is updated only
    while it still holds what was read, so a stamp a writer puts there meanwhile stands."""
    found = {column: _local_rows(conn, column) for column, _ in COLUMNS}
    ids = sorted(set().union(*found.values()))
    stats: dict = {column: len(rows) for column, rows in found.items()}
    stats.update(rows=len(ids), updated=0, changed_meanwhile=0, sample=None)
    if ids:
        ac_id = ids[0]
        first = found["extracted_at"].get(ac_id) or found["llm_extracted_at"][ac_id]
        stats["sample"] = (first, to_utc(first))

    meta = dict(
        conn.execute(
            "SELECT key, value FROM sync_metadata WHERE key IN (?, ?) OR key LIKE ?",
            (*SYNC_KEYS, f"{REFETCH_PREFIX}%"),
        )
    )
    sync = {k: new for k in SYNC_KEYS if k in meta and (new := _utc_iso(meta[k]))}
    records = {}
    for key, value in meta.items():
        if not key.startswith(REFETCH_PREFIX):
            continue
        try:
            record = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        changed = {f: new for f in REFETCH_FIELDS if (new := _utc_iso(record.get(f)))}
        if changed:
            records[key] = json.dumps({**record, **changed})
    stats.update(sync_metadata=len(sync), refetch_records=len(records))
    if not apply:
        return stats

    stats["unreadable"] = 0
    for start in range(0, len(ids), CHUNK):
        for ac_id in ids[start : start + CHUNK]:
            olds = [(column, found[column].get(ac_id)) for column, _ in COLUMNS]
            changes = [(c, old, new) for c, old in olds if old and (new := to_utc(old))]
            if not changes:
                stats["unreadable"] += 1
                continue
            sets = ", ".join(f"{c} = ?" for c, _, _ in changes)
            same = " AND ".join(f"{c} = ?" for c, _, _ in changes)
            done = conn.execute(
                f"UPDATE attachment_content SET {sets} WHERE id = ? AND {same}",
                [new for _, _, new in changes] + [ac_id] + [old for _, old, _ in changes],
            ).rowcount
            stats["updated" if done else "changed_meanwhile"] += 1
        conn.commit()
    conn.executemany(
        "UPDATE sync_metadata SET value = ? WHERE key = ? AND value = ?",
        [(new, key, meta[key]) for key, new in {**sync, **records}.items()],
    )
    conn.commit()
    return stats


def cmd(args) -> int:
    """`brain maint utc-stamps [--apply]`: a dry run unless --apply."""
    from src.store.schema import get_connection, run_migrations

    conn = get_connection(str(args.db))
    try:
        if args.apply:
            run_migrations(conn)
        stats = convert(conn, apply=args.apply)
    finally:
        conn.close()
    print(f"utc-stamps {'APPLIED' if args.apply else 'DRY RUN (nothing written)'}:")
    print(f"  {'attachment_content.extracted_at':<36}: {stats['extracted_at']:,} in local time")
    print(f"  {'attachment_content.llm_extracted_at':<36}: {stats['llm_extracted_at']:,}")
    print(f"  {'sync_metadata stamps':<36}: {stats['sync_metadata']:,}")
    print(f"  {'refetch-garbled records':<36}: {stats['refetch_records']:,}")
    if stats["sample"]:
        print(f"  {'for example':<36}: {stats['sample'][0]} -> {stats['sample'][1]}")
    if args.apply:
        print(f"  {'rows updated':<36}: {stats['updated']:,}")
        if stats["changed_meanwhile"]:
            print(f"  {'rows a writer stamped meanwhile':<36}: {stats['changed_meanwhile']:,}")
        if stats["unreadable"]:
            print(f"  {'rows whose stamps do not parse':<36}: {stats['unreadable']:,}")
    else:
        print("Run it again with --apply to convert them.")
    return 0

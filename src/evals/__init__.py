"""Evals: frozen sets that measure extraction and retrieval, kept outside the repository.

No metric guarded either until 2026-10: three audits changed tokenising, embeddings,
Greek folding and ranking with no number to say better or worse, and the one eval was
a keyword-only known-item check whose results were never kept (audit 2026-10-11).
scripts/extraction_eval.py and scripts/retrieval_eval.py build, run and grade the sets;
summary.py reads their results back for the health check.

The sets are the owner's own mail and questions, and their gold labels are the owner's
judgement, so neither ever enters the repository: everything lives under
``${BRAIN_DATA_DIR:-~/.second-brain}/evals``, with the results beside the sets.
"""

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# A drop larger than this many percentage points against the previous comparable run
# is reported as a warning by summary.latest_eval_summary.
DROP_POINTS = 5.0


def eval_base() -> Path:
    """$BRAIN_DATA_DIR when set, else ~/.second-brain: outside any checkout either way.

    Read at each call, not at import, so a run or a test that sets it is honoured.
    """
    return Path(os.environ.get("BRAIN_DATA_DIR") or Path.home() / ".second-brain")


def eval_home(base: Path | str | None = None) -> Path:
    """The directory that holds every eval set and its results."""
    return Path(base or eval_base()) / "evals"


def read_only_uri(db: Path | str) -> str:
    """The URI that opens `db` read-only, and immutable on a replica.

    Nothing writes a replica but the pull, which replaces the file, and plain read-only
    opening has failed on the Mac replica. The producer's store is live, so there the
    locks stay on: immutable would read past the WAL a writer is filling.
    """
    from src.config import is_replica

    uri = f"{Path(db).resolve().as_uri()}?mode=ro"
    return f"{uri}&immutable=1" if is_replica() else uri


def connect_read_only(db: Path | str) -> sqlite3.Connection:
    """A connection that cannot write, and cannot create a missing store either."""
    conn = sqlite3.connect(read_only_uri(db), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_jsonl(path: Path | str, *, strict: bool = True) -> list[dict]:
    """Every JSON object in `path`, one per line; blank lines are skipped.

    Strict, a line that is not an object raises ValueError naming its number (never
    its text: a case file holds mail). Lenient, it is skipped: a results file is read
    for a summary, and one torn line must not hide every run before it.
    """
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as e:
                if strict:
                    raise ValueError(f"{path}:{n}: not valid JSON ({e.msg})") from None
                continue
            if isinstance(value, dict):
                out.append(value)
            elif strict:
                raise ValueError(f"{path}:{n}: not a JSON object")
    return out


def _line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n"


def append_jsonl(path: Path | str, record: dict) -> None:
    """Append one record, creating the directory on first use."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_line(record))


def write_jsonl(path: Path | str, records: list[dict]) -> None:
    """Replace `path` with `records`, through a temporary file, so a reader never sees half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(_line(r) for r in records)
    tmp.replace(path)


def text_sha(text: str) -> str:
    """A short, stable fingerprint of `text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def file_sha(path: Path | str) -> str:
    """A short fingerprint of a file's bytes: two runs on the same set share it."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def stamp(now: datetime | None = None) -> dict:
    """The `ts` (UTC, to the second) and `date` every results record carries."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return {"ts": now.isoformat(timespec="seconds"), "date": now.date().isoformat()}

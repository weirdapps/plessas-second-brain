"""Adapter that answers outlook-cli's mail verbs through a Microsoft Graph CLI.

An alternative backend for `run_outlook_cli`, chosen with SB_MAIL_BACKEND (see
outlook_cli.py). The Graph CLI is a separate binary set by GRAPH_CLI_PATH, signed in
with its own app registration and refresh token, so it renews without a browser. Its
command shape:

    mail list --folder F [--after T] [--before T] --top N [--skip N] --json
        -> {"data": [{"id", "date", "subject", "from", "read", "attachments", ...}],
            "nextSkip": N | null}
    mail read <id> --full --json              -> {"data": <Graph message, camelCase>}
    mail attachments list <id> --json         -> {"data": [{"name", "inline", "contentId"}]}
    mail attachments download <id> --out D [--force] --json
                                              -> {"data": {"saved": [{"name", "path"}]}}
    status --json                             -> exit 0 when signed in
    exit 2 means sign in again; exit 1 is every other failure.

Callers read outlook-cli's output: Outlook REST v2.0, PascalCase. Graph has the same
schema in camelCase and the same Exchange item ids, so the translation is a key
rename plus a few verb-level emulations below. What it cannot answer faithfully raises
GraphCliUnsupported, and the dispatcher sends that call to outlook-cli instead:

  - calendar verbs: the Graph CLI's event read leaves out IsCancelled, SeriesMasterId,
    Type and IsAllDay, which the calendar sync stores;
  - get-mail --body none: outlook-cli answers it with the attachment list embedded,
    and the Graph read has none, so an empty list would read as "no attachments";
  - a listing whose fields go beyond the native ones and which is larger than
    SB_GRAPH_ENRICH_MAX (default 2000): each such message costs one read.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PAGE = 1000  # the Graph CLI's largest page
DEFAULT_ENRICH_MAX = 2000
_CACHE_MAX = 4000

# Fields a listing row carries without a read, and how to get each from it.
_NATIVE = {
    "Id": lambda r: r.get("id"),
    "ReceivedDateTime": lambda r: r.get("date"),
    "Subject": lambda r: r.get("subject"),
    "HasAttachments": lambda r: r.get("attachments") == "yes",
    "IsRead": lambda r: r.get("read") == "yes",
}

# Full reads kept for the get-mail that follows a listing in the same run.
_READ_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


class GraphCliError(Exception):
    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"graph cli exit {exit_code}: {stderr}")


class GraphCliAuthRequired(GraphCliError):
    """Exit 2: signing in again fixes it."""

    def __init__(self, stderr: str):
        super().__init__(2, stderr)


class GraphCliUnsupported(GraphCliError):
    """A call this adapter cannot answer in outlook-cli's shape."""

    def __init__(self, reason: str):
        super().__init__(-1, reason)


def _binary() -> str:
    path = os.environ.get("GRAPH_CLI_PATH")
    if not path:
        raise GraphCliUnsupported("GRAPH_CLI_PATH is not set")
    return path


def _spawn(cmd: list[str], timeout_sec: int, env: dict) -> subprocess.CompletedProcess:
    """Run the CLI with stdout going to a file, never a pipe.

    Into a pipe, the Graph CLI sometimes exits 0 having written only the first 64 KiB
    (2 runs in 5 on a 168 KB listing, 2026-11-06); into a file it wrote all of it 20
    times in 20. A cut listing never parses, so it would fail loudly, but it would
    fail every large page at random."""
    with tempfile.TemporaryFile() as out:
        res = subprocess.run(
            cmd, stdout=out, stderr=subprocess.PIPE, timeout=timeout_sec, check=False, env=env
        )
        out.seek(0)
        stdout = out.read().decode("utf-8", errors="replace")
    stderr = (
        res.stderr.decode("utf-8", errors="replace")
        if isinstance(res.stderr, bytes)
        else (res.stderr or "")
    )
    return subprocess.CompletedProcess(cmd, res.returncode, stdout, stderr)


def _run(args: list[str], timeout_sec: int, unwrap: bool = True) -> Any:
    cmd = [_binary(), *args, "--json"]
    # Plain dates are read in the CLI's own zone; every bound we pass carries a Z,
    # and TZ pins the rest, so the window means the same on every host.
    env = {**os.environ, "TZ": "UTC"}
    try:
        res = _spawn(cmd, timeout_sec=timeout_sec, env=env)
    except FileNotFoundError as e:
        raise GraphCliUnsupported(f"graph cli not found at {cmd[0]}") from e
    if res.returncode == 2:
        raise GraphCliAuthRequired(res.stderr.strip())
    if res.returncode != 0:
        raise GraphCliError(res.returncode, res.stderr.strip())
    try:
        out = json.loads(res.stdout)
    except json.JSONDecodeError:
        out = json.JSONDecoder().raw_decode(res.stdout.lstrip())[0]
    if unwrap and isinstance(out, dict) and "data" in out:
        return out["data"]
    return out


def _pascal(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            (k if k.startswith("@") else k[:1].upper() + k[1:]): _pascal(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_pascal(v) for v in value]
    return value


def _to_outlook(msg: dict) -> dict:
    out = _pascal(msg)
    # Enum values are camelCase in Graph and PascalCase in Outlook REST v2.0.
    for key in ("Importance", "InferenceClassification"):
        if isinstance(out.get(key), str):
            out[key] = out[key][:1].upper() + out[key][1:]
    flag = out.get("Flag")
    if isinstance(flag, dict) and isinstance(flag.get("FlagStatus"), str):
        flag["FlagStatus"] = flag["FlagStatus"][:1].upper() + flag["FlagStatus"][1:]
    body = out.get("Body")
    if isinstance(body, dict) and isinstance(body.get("ContentType"), str):
        body["ContentType"] = {"html": "HTML", "text": "Text"}.get(
            body["ContentType"].lower(), body["ContentType"]
        )
    return out


def _utc(stamp: str) -> str:
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flags(args: list[str]) -> tuple[list[str], dict[str, str | bool]]:
    positional: list[str] = []
    flags: dict[str, str | bool] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                flags[a[2:]] = args[i + 1]
                i += 2
                continue
            flags[a[2:]] = True
        else:
            positional.append(a)
        i += 1
    return positional, flags


def _read(message_id: str, timeout_sec: int) -> dict:
    with _CACHE_LOCK:
        hit = _READ_CACHE.get(message_id)
    if hit is not None:
        return hit
    msg = _to_outlook(_run(["mail", "read", message_id, "--full"], timeout_sec))
    with _CACHE_LOCK:
        if len(_READ_CACHE) >= _CACHE_MAX:
            _READ_CACHE.clear()
        _READ_CACHE[message_id] = msg
    return msg


def _get_mail(pos: list[str], flags: dict, timeout_sec: int) -> dict:
    body = flags.get("body", "html")
    if body != "html":
        raise GraphCliUnsupported(f"get-mail --body {body}")
    return dict(_read(pos[0], timeout_sec))


def _list_mail(flags: dict, timeout_sec: int) -> list[dict]:
    fields = [f for f in str(flags.get("select") or "Id").split(",") if f]
    folder = str(flags.get("folder") or "Inbox")
    base = ["mail", "list", "--folder", folder]
    if isinstance(flags.get("since"), str):
        base += ["--after", _utc(flags["since"])]
    if isinstance(flags.get("until"), str):
        base += ["--before", _utc(flags["until"])]

    if flags.get("all"):
        limit = int(flags.get("max") or 100_000)
        page = PAGE
    else:
        limit = int(flags.get("top") or 10)
        page = min(limit, PAGE)

    rows: list[dict] = []
    seen: set[str] = set()
    skip = 0
    while len(rows) < limit:
        args = [*base, "--top", str(page)] + (["--skip", str(skip)] if skip else [])
        out = _run(args, timeout_sec, unwrap=False)
        data = out.get("data", []) if isinstance(out, dict) else out
        for r in data or []:
            if r.get("id") and r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)
        nxt = out.get("nextSkip") if isinstance(out, dict) else None
        if not flags.get("all") or not nxt or not data:
            break
        skip = int(nxt)
    rows = rows[:limit]

    if all(f in _NATIVE for f in fields):
        return [{f: _NATIVE[f](r) for f in fields} for r in rows]

    cap = int(os.environ.get("SB_GRAPH_ENRICH_MAX") or DEFAULT_ENRICH_MAX)
    if len(rows) > cap:
        raise GraphCliUnsupported(f"{len(rows)} messages need a read each (cap {cap})")
    return [{f: _read(r["id"], timeout_sec).get(f) for f in fields} for r in rows]


def _download(pos: list[str], flags: dict, timeout_sec: int) -> dict:
    msg_id = pos[0]
    out_dir = str(flags.get("out") or ".")
    inline: set[str] = set()
    if not flags.get("include-inline"):
        listed = _run(["mail", "attachments", "list", msg_id], timeout_sec) or []
        inline = {a.get("name") for a in listed if a.get("inline") == "yes"}
        regular = {a.get("name") for a in listed if a.get("inline") != "yes"}
        inline -= regular  # a name shared with a real attachment is kept
    args = ["mail", "attachments", "download", msg_id, "--out", out_dir]
    if flags.get("overwrite"):
        args.append("--force")
    res = _run(args, timeout_sec) or {}
    saved, skipped = [], []
    for s in res.get("saved", []) if isinstance(res, dict) else []:
        path = Path(s.get("path") or Path(out_dir) / s.get("name", ""))
        if s.get("name") in inline:
            path.unlink(missing_ok=True)
            skipped.append({"name": s.get("name"), "reason": "inline"})
            continue
        size = path.stat().st_size if path.exists() else s.get("size")
        saved.append({"name": s.get("name"), "path": str(path), "size": size})
    return {"messageId": msg_id, "outDir": out_dir, "saved": saved, "skipped": skipped}


def translate(args: list[str], timeout_sec: int = 60) -> Any:
    """Answer one outlook-cli invocation through the Graph CLI, in outlook-cli's shape."""
    if not args:
        raise GraphCliUnsupported("no verb")
    verb, rest = args[0], [a for a in args[1:] if a not in ("--json", "--no-auto-reauth")]
    _binary()
    pos, flags = _flags(rest)
    if verb == "auth-check":
        _run(["status"], timeout_sec)
        return {"ok": True}
    if verb == "get-mail" and pos:
        return _get_mail(pos, flags, timeout_sec)
    if verb == "list-mail":
        return _list_mail(flags, timeout_sec)
    if verb == "download-attachments" and pos:
        return _download(pos, flags, timeout_sec)
    raise GraphCliUnsupported(verb)

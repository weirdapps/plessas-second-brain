"""
Subprocess wrapper for outlook-cli.

Used by the second-brain ingestion pipeline. Always passes --no-auto-reauth
to prevent silent Playwright pop-ups in cron context.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _resolve_outlook_cli() -> str:
    """Absolute path to outlook-cli, not a bare name on the caller's PATH.

    The binary lives in ~/.local/bin, which is on an interactive shell's PATH and
    frequently not on a daemon's. MCP servers in particular run with a sanitized
    PATH, so `outlook_live_search` raised a bare FileNotFoundError in most live
    server processes: the one tool that exists to reach past a stale replica was
    the one that could not run. Same override shape as SHAREPOINT_CLI_PATH.
    """
    override = os.environ.get("OUTLOOK_CLI_PATH")
    if override:
        return override
    found = shutil.which("outlook-cli")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "outlook-cli")


class OutlookCliError(Exception):
    def __init__(self, exit_code: int, stderr: str, retryable: bool):
        self.exit_code = exit_code
        self.stderr = stderr
        self.retryable = retryable
        super().__init__(f"outlook-cli exit {exit_code}: {stderr}")


class OutlookCliAuthRequired(OutlookCliError):
    """Subclass for exit code 4 — caller should bail without retrying."""

    def __init__(self, stderr: str):
        super().__init__(exit_code=4, stderr=stderr, retryable=False)


_RETRYABLE_EXIT_CODES = {5}  # upstream API errors


_BACKENDS = {"outlook-cli", "graph", "auto", "shadow"}
_SHADOWED = {"list-mail", "get-mail"}


def _backend() -> str:
    name = (os.environ.get("SB_MAIL_BACKEND") or "outlook-cli").strip().lower()
    return name if name in _BACKENDS else "outlook-cli"


def _record(event: dict) -> None:
    """One line per fallback or shadow comparison, for the health checks. Never raises."""
    try:
        from datetime import UTC, datetime

        from src import config

        event = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **event}
        path = config.DATA_ROOT / "mail_backend_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception:
        pass


def _compare(verb: str, ours: Any, theirs: Any) -> dict:
    """What a shadow read found: ids only, never content."""
    if verb == "list-mail":
        a = {m["Id"] for m in ours or [] if isinstance(m, dict) and m.get("Id")}
        b = {m["Id"] for m in theirs or [] if isinstance(m, dict) and m.get("Id")}
        if a == b:
            return {"outcome": "match", "count": len(a)}
        return {
            "outcome": "mismatch",
            "count": len(a),
            "only_outlook_cli": sorted(a - b)[:50],
            "only_graph": sorted(b - a)[:50],
        }
    keys = ("Id", "InternetMessageId", "ConversationId", "Subject")
    diff = [k for k in keys if (ours or {}).get(k) != (theirs or {}).get(k)]
    a_len = len(((ours or {}).get("Body") or {}).get("Content") or "")
    b_len = len(((theirs or {}).get("Body") or {}).get("Content") or "")
    if a_len != b_len:
        diff.append("Body")
    return {"outcome": "mismatch", "fields": diff} if diff else {"outcome": "match"}


def run_outlook_cli(args: list[str], timeout_sec: int = 60) -> Any:
    """
    Answer an outlook-cli invocation through the backend SB_MAIL_BACKEND names.

    outlook-cli (default, and any unknown name): outlook-cli only.
    graph:  the Graph CLI (graph_cli.py); a call it cannot serve goes to outlook-cli,
            and its errors come back as this module's error types.
    auto:   the Graph CLI first, outlook-cli for that call whenever it fails.
    shadow: outlook-cli answers; reads are repeated through the Graph CLI and the
            comparison recorded. Downloads are never repeated.

    Either way the result is outlook-cli's JSON shape, and message ids are the same
    on both, so switching backends mid-run never duplicates a row.
    """
    backend = _backend()
    if backend == "outlook-cli":
        return _run_binary(args, timeout_sec)

    from src.export import graph_cli

    verb = args[0] if args else ""
    if backend == "shadow":
        result = _run_binary(args, timeout_sec)
        if verb in _SHADOWED:
            try:
                theirs = graph_cli.translate(args, timeout_sec)
                _record({"backend": "shadow", "verb": verb, **_compare(verb, result, theirs)})
            except graph_cli.GraphCliUnsupported:
                pass
            except Exception as e:  # a shadow must never cost the real call
                _record(
                    {
                        "backend": "shadow",
                        "verb": verb,
                        "outcome": "graph_error",
                        "error": str(e)[:300],
                    }
                )
        return result

    if backend == "auto":
        return _auto(args, verb, timeout_sec)

    try:
        return graph_cli.translate(args, timeout_sec)
    except graph_cli.GraphCliUnsupported:
        return _run_binary(args, timeout_sec)
    except (graph_cli.GraphCliError, subprocess.TimeoutExpired) as e:
        if isinstance(e, graph_cli.GraphCliAuthRequired):
            raise OutlookCliAuthRequired(f"graph cli needs a new sign-in: {e.stderr}") from e
        if isinstance(e, subprocess.TimeoutExpired):
            raise
        raise OutlookCliError(exit_code=e.exit_code, stderr=e.stderr, retryable=False) from e


def _fallback_reason(e: Exception) -> str:
    """auth: the Graph CLI exited 2, sign it in again. error: it exited 1 (network,
    Graph, anything else it reports). timeout: it never answered. unexpected: the
    adapter itself broke (a parse or shape bug), which is ours to fix."""
    from src.export import graph_cli

    if isinstance(e, graph_cli.GraphCliAuthRequired):
        return "auth"
    if isinstance(e, graph_cli.GraphCliError):
        return "error"
    if isinstance(e, subprocess.TimeoutExpired):
        return "timeout"
    return "unexpected"


def _auto(args: list[str], verb: str, timeout_sec: int) -> Any:
    """The Graph CLI first; outlook-cli for this call whenever it fails, for any reason.

    Every call the Graph CLI was asked to serve leaves one line, `ok` or `fallback`, so
    `brain mail-backend-report` can give the fallback rate. A call it cannot serve by
    design (GraphCliUnsupported: calendar verbs, --body none) goes to outlook-cli
    unrecorded, since it is no failure and a line each would bury the real ones."""
    from src.export import graph_cli

    try:
        result = graph_cli.translate(args, timeout_sec)
    except graph_cli.GraphCliUnsupported:
        return _run_binary(args, timeout_sec)
    except Exception as e:  # a Graph failure must cost one fallback, never the call
        event: dict[str, Any] = {
            "backend": "auto",
            "verb": verb,
            "outcome": "fallback",
            "reason": _fallback_reason(e),
            "error": f"{type(e).__name__}: {e}"[:300],
        }
        if isinstance(e, graph_cli.GraphCliError):
            event["exit_code"] = e.exit_code
        _record(event)
        return _run_binary(args, timeout_sec)
    _record({"backend": "auto", "verb": verb, "outcome": "ok"})
    return result


def _run_binary(args: list[str], timeout_sec: int = 60) -> Any:
    """
    Invoke outlook-cli with the given args. Always appends --no-auto-reauth
    and --json. Returns parsed JSON on exit 0; raises typed errors otherwise.
    """
    final_args = list(args)
    if "--no-auto-reauth" not in final_args:
        final_args.append("--no-auto-reauth")
    if "--json" not in final_args:
        final_args.append("--json")

    cmd = [_resolve_outlook_cli(), *final_args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError as e:
        # A typed error saying what to do, not a bare FileNotFoundError that a
        # caller reads as "no results".
        raise OutlookCliError(
            exit_code=127,
            stderr=(
                f"outlook-cli not found at {cmd[0]}. Install it, or set "
                "OUTLOOK_CLI_PATH to its absolute path (this process does not "
                "necessarily share an interactive shell's PATH)."
            ),
            retryable=False,
        ) from e

    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            # outlook-cli occasionally appends a non-JSON line after the JSON
            # payload (e.g. a token-refresh notice), making json.loads raise
            # "Extra data". Decode just the leading JSON value and ignore the
            # trailing output rather than failing the whole sync.
            return json.JSONDecoder().raw_decode(result.stdout.lstrip())[0]

    if result.returncode == 4:
        raise OutlookCliAuthRequired(result.stderr.strip())

    raise OutlookCliError(
        exit_code=result.returncode,
        stderr=result.stderr.strip(),
        retryable=result.returncode in _RETRYABLE_EXIT_CODES,
    )

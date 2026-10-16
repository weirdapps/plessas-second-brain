"""A character budget for MCP tool results.

Claude Code saves a tool result longer than 50,000 characters to a file and hands
the model a path instead, so an oversized answer costs a file round trip, and the
fields at its end are what the preview of the saved file leaves out. recall
crossed that line in a quarter of real calls, and other list tools reached
100,000 to 950,000 characters at their row caps: rows were bounded, characters
were not. budget_response bounds the characters.

detail="concise" asks for less again: concise() keeps one short line per row, and
the answer is held to CONCISE_BUDGET_CHARS.
"""

from typing import Any

import pydantic_core

# Under the 50,000 at which Claude Code stops showing a result inline, with room
# for what a client adds around it.
RESPONSE_BUDGET_CHARS = 40_000

# A concise answer. On a replica's real queries, recall rows cut to the fields below
# came to 7-15K characters at limit_per_kind 5 to 8, against 27-55K in full.
CONCISE_BUDGET_CHARS = 10_000
# What a concise row keeps: what it is and where to read it (the row envelope), the
# flags and people a caller acts on, and counts.
CONCISE_KEYS = frozenset(
    {
        "kind",
        "ref",
        "date",
        "who",
        "partial_match",
        "overdue",
        "owner",
        "deadline",
        "status",
        "decided_by",
        "by_person",
        "count",
        "email_count",
        "message_count",
        "thread_matches",
        "older_editions",
    }
)
# ...and the first of these it has, cut to CONCISE_TITLE_CHARS: the line that says
# what the item is.
CONCISE_TITLES = (
    "subject",
    "filename",
    "title",
    "decision",
    "task",
    "commitment",
    "fact",
    "topic",
    "name",
    "chat_name",
    "vision_description",
    "summary",
)
CONCISE_TITLE_CHARS = 80
# The person the row names already; its email's sender is then left out.
_ACTORS = ("decided_by", "owner", "by_person")

# The SDK sends a result's text as JSON indented by two spaces, the larger of the
# two forms a client may show (structuredContent is compact).
_INDENT = 2


def json_chars(value: Any) -> int:
    """Characters in `value` as the server sends it: JSON indented by two spaces."""
    return len(pydantic_core.to_json(value, fallback=str, indent=_INDENT).decode())


def _buckets(obj: dict, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], list]]:
    """Every list of rows (dicts) in `obj`, at any depth of dicts, with its key path.

    Lists of plain values (a summary's kind names) are not rows, and nothing
    inside a row is a bucket: a row is kept or dropped whole.
    """
    found: list[tuple[tuple[str, ...], list]] = []
    for key, value in obj.items():
        if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
            found.append(((*path, key), value))
        elif isinstance(value, dict):
            found.extend(_buckets(value, (*path, key)))
    return found


def _row_chars(row: Any, depth: int) -> int:
    """About what one row adds to the indented JSON at `depth`: its own text, each
    line indented by its depth, and the separator."""
    text = pydantic_core.to_json(row, fallback=str, indent=_INDENT).decode()
    return len(text) + (text.count("\n") + 1) * _INDENT * depth + 2


def _with_rows(obj: dict, path: tuple[str, ...], rows: list) -> dict:
    """A shallow copy of `obj` with the list at `path` replaced by `rows`."""
    head, *rest = path
    copy = dict(obj)
    copy[head] = _with_rows(obj[head], tuple(rest), rows) if rest else rows
    return copy


def _short_who(who: Any) -> Any:
    """'Name <address>' as its name alone; anything else as it is."""
    if isinstance(who, str) and "<" in who and not who.startswith("<"):
        return who.split("<", 1)[0].strip()
    return who


def concise_row(row: dict) -> dict:
    """One row as a short line: CONCISE_KEYS, then its first title, cut to
    CONCISE_TITLE_CHARS. `who` gives the name alone, and is left out where the row
    already names who decided, owns or committed."""
    out = {key: value for key, value in row.items() if key in CONCISE_KEYS}
    if any(row.get(actor) for actor in _ACTORS):
        out.pop("who", None)
    elif "who" in out:
        out["who"] = _short_who(out["who"])
    for key in CONCISE_TITLES:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            if len(text) > CONCISE_TITLE_CHARS:
                text = text[: CONCISE_TITLE_CHARS - 1] + "…"
            out[key] = text
            break
    return out


def concise_rows(rows: list[dict]) -> list[dict]:
    """Each row as concise_row gives it."""
    return [concise_row(row) for row in rows]


# Lists that are not rows to shorten: the next people a name could be, each one's
# name, address and identifier, at most three.
_CONCISE_AS_IS = frozenset({"other_candidates"})


def _is_container(value: dict) -> bool:
    """A dict in a list that is no row (it has no `ref`) and holds lists itself: a
    meeting_prep attendee's dossier, not a search hit."""
    return "ref" not in value and any(isinstance(v, list) for v in value.values())


def concise(obj: dict) -> dict:
    """`obj` with every list of rows, at any depth of dicts, cut to concise rows.

    Scalars and small dicts (a person, a summary, a pattern) stay as they are; a list
    whose items are dossiers rather than rows has each dossier made concise in turn.
    """
    out: dict = {}
    for key, value in obj.items():
        if key in _CONCISE_AS_IS:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = concise(value)
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            if any(_is_container(v) for v in value):
                out[key] = [concise(v) for v in value]
            else:
                out[key] = concise_rows(value)
        else:
            out[key] = value
    return out


def budget_table(obj: dict, max_chars: int = RESPONSE_BUDGET_CHARS) -> dict:
    """sql_query's answer with rows cut from the tail of `rows` until its JSON fits.

    Its rows are lists of values, not dicts, and its `truncated` is a flag that has
    always meant "something was left out", so it stays one: true when a row goes
    here, and `row_count` follows the rows kept.
    """
    size = json_chars(obj)
    rows = obj.get("rows") or []
    if size <= max_chars or not rows:
        return obj
    kept = len(rows)
    while kept and size > max_chars:
        kept -= 1
        size -= _row_chars(rows[kept], 2)
    out = {**obj, "rows": rows[:kept], "row_count": kept, "truncated": True}
    while out["rows"] and json_chars(out) > max_chars:  # the estimate fell short
        out["rows"] = out["rows"][:-1]
        out["row_count"] = len(out["rows"])
    return out


def budget_response(obj: dict, max_chars: int = RESPONSE_BUDGET_CHARS) -> dict:
    """`obj` with rows cut from the tail of its lists until its JSON fits `max_chars`.

    A row goes from whichever list holds the most characters at the time, so one
    heavy list shrinks before a light one loses its few rows; among equals, the
    later list gives first. Each list that lost rows is named in `truncated`, by
    its key or its dotted path inside a nested dict, as {"kept": n, "total": m}.
    A result that fits comes back as it is; the input is never modified. When even
    every row is not enough, the rows are all gone and `truncated` says so.
    """
    size = json_chars(obj)
    if size <= max_chars:
        return obj
    buckets = _buckets(obj)
    if not buckets:
        return obj
    row_chars = {path: [_row_chars(row, len(path) + 1) for row in rows] for path, rows in buckets}
    kept = {path: len(rows) for path, rows in buckets}
    weight = {path: sum(sizes) for path, sizes in row_chars.items()}
    while True:
        over = size - max_chars
        while over > 0:
            heaviest = None
            for path in kept:  # in order, so a later list wins a tie
                if kept[path] and (heaviest is None or weight[path] >= weight[heaviest]):
                    heaviest = path
            if heaviest is None:
                break
            kept[heaviest] -= 1
            dropped = row_chars[heaviest][kept[heaviest]]
            weight[heaviest] -= dropped
            over -= dropped
        out = dict(obj)
        for path, rows in buckets:
            if kept[path] < len(rows):
                out = _with_rows(out, path, rows[: kept[path]])
        out["truncated"] = {
            ".".join(path): {"kept": kept[path], "total": len(rows)}
            for path, rows in buckets
            if kept[path] < len(rows)
        }
        size = json_chars(out)
        if size <= max_chars or not any(kept.values()):
            return out

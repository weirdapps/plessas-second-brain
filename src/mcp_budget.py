"""A character budget for MCP tool results.

Claude Code saves a tool result longer than 50,000 characters to a file and hands
the model a path instead, so an oversized answer costs a file round trip, and the
fields at its end are what the preview of the saved file leaves out. recall
crossed that line in a quarter of real calls, and other list tools reached
100,000 to 950,000 characters at their row caps: rows were bounded, characters
were not. budget_response bounds the characters.
"""

from typing import Any

import pydantic_core

# Under the 50,000 at which Claude Code stops showing a result inline, with room
# for what a client adds around it.
RESPONSE_BUDGET_CHARS = 40_000

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


def _row_chars(row: dict, depth: int) -> int:
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

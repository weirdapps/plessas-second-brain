"""The shape of every tool's result, held in tests/fixtures/mcp_shapes.json.

The Telegram bot and the marketplace skills read these results, and nothing checked
what they could rely on: freshness travelled with one tool of 25, a search row's
`source` meant the field that matched, and a key renamed in one tool reached them
unannounced. This holds, per tool, the output schema's keys as `tools/list` gives
them, the keys of a sample result in order, and the keys of every list of rows in
it, read against a store holding an item of every kind (tests/seeded_store.py).

A change to any of them fails here until the snapshot is regenerated, and the diff
of that file is the review of the change:

    SNAPSHOT_UPDATE=1 uv run pytest tests/test_mcp_shapes.py

Add fields; rename or remove one only with a release of notice (README, MCP tools).
"""

import json
import os
from pathlib import Path

import pytest

from src import mcp_server
from tests import seeded_store
from tests.mcp_listing import listed_tools

SNAPSHOT = Path(__file__).parent / "fixtures" / "mcp_shapes.json"


def _rows(value, path: str, found: dict) -> None:
    """The keys of every list of rows inside `value`, at any depth, by dotted path."""
    if isinstance(value, dict):
        for key, inner in value.items():
            _rows(inner, f"{path}.{key}" if path else key, found)
    elif isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        found[path] = sorted({key for row in value for key in row} | set(found.get(path, [])))
        for row in value:
            for key, inner in row.items():
                _rows(inner, f"{path}[].{key}", found)


def _shape(name: str, result: dict) -> dict:
    schema = listed_tools()[name].output_schema or {}
    rows: dict = {}
    _rows(result, "", rows)
    return {
        "output_schema": {
            "properties": sorted(schema.get("properties", {})),
            "required": sorted(schema.get("required", [])),
        },
        "result_keys": list(result),
        "rows": dict(sorted(rows.items())),
    }


@pytest.fixture(scope="module")
def shapes(tmp_path_factory):
    """Every tool's shape, from one store; the owner's address unset, as on CI."""
    from src import config
    from src.store import query

    patch = pytest.MonkeyPatch()
    try:
        path = seeded_store.build(tmp_path_factory.mktemp("shapes") / "brain.db")
        seeded_store.serve(path, patch)
        patch.setattr(config, "USER_EMAIL_PATTERN", "")
        patch.setattr(query, "USER_EMAIL_PATTERN", "")
        return {
            name: _shape(name, getattr(mcp_server, name)(**seeded_store.CALLS[name]))
            for name in sorted(seeded_store.CALLS)
        }
    finally:
        patch.undo()


def test_the_sample_calls_cover_every_tool():
    assert set(seeded_store.CALLS) == set(listed_tools())


def test_the_snapshot_names_every_tool(shapes):
    if os.environ.get("SNAPSHOT_UPDATE"):
        SNAPSHOT.write_text(json.dumps(shapes, indent=2, ensure_ascii=False) + "\n")
    assert sorted(json.loads(SNAPSHOT.read_text())) == sorted(shapes)


@pytest.mark.parametrize("name", sorted(seeded_store.CALLS))
def test_each_tools_result_keeps_its_shape(shapes, name):
    held = json.loads(SNAPSHOT.read_text()).get(name)

    assert shapes[name] == held, (
        f"{name}'s result shape changed. If that is intended, regenerate with "
        "SNAPSHOT_UPDATE=1 and review the diff of tests/fixtures/mcp_shapes.json."
    )

"""The MCP server loads, at start, every module its tools import later.

A tool body that imports a src module the server had not loaded yet picked up
whatever had been written to disk since the server started, beside the old copies
in memory. On 2026-10-11 that broke search_emails and search_attachments in every
running session: the new query.py asked the old greek.py for query_stem, and the
ImportError reached the client as a bare "Error executing tool". The server now
loads that whole set at import, and this test fails when a new function-level
import is not on its list.
"""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _nested_src_imports(module: str) -> set[str]:
    """Src modules imported inside function bodies of one module."""
    spec = importlib.util.find_spec(module)
    assert spec and spec.origin
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    found: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src"):
                found.add(node.module)
                # `from src.store import sql_readonly` imports a module, not a name.
                package = importlib.util.find_spec(node.module)
                if package and package.submodule_search_locations:
                    for alias in node.names:
                        if importlib.util.find_spec(f"{node.module}.{alias.name}"):
                            found.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.startswith("src"))
    return found


def _reachable() -> set[str]:
    """Nested imports of the server, and of those modules, and so on."""
    todo: list[str] = ["src.mcp_server"]
    seen: set[str] = set()
    while todo:
        for target in _nested_src_imports(todo.pop()) - seen:
            seen.add(target)
            todo.append(target)
    return seen


def test_server_import_loads_every_module_its_tools_import_later():
    # A fresh interpreter: other tests have already imported most of src.
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys, src.mcp_server; print(json.dumps(sorted(sys.modules)))",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    missing = sorted(_reachable() - loaded)
    assert not missing, f"add to the eager list in src/mcp_server.py: {missing}"

"""Patching helpers that fail when what they patch is gone.

`monkeypatch.delenv(name, raising=False)` was the suite's way to clear a variable a
developer's shell may or may not export. It cannot tell a variable that is merely
unset from one the code stopped reading, and after a rename the test goes on passing
while it isolates nothing. tests/test_strict_patches.py keeps `raising=False` out of
the suite; this is what replaces it for the environment.
"""

import os
import re
from functools import cache
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
# Where a setting can be read: the package, the scripts and wrappers, and the two
# launchers at the top of the repo.
_CODE_DIRS = ("src", "scripts")
_CODE_FILES = ("run_mcp.sh", "brain")


@cache
def _code_text() -> str:
    parts = [
        path.read_text(encoding="utf-8", errors="replace")
        for top in _CODE_DIRS
        for path in sorted((_REPO / top).rglob("*"))
        if path.suffix in (".py", ".sh") and path.is_file()
    ]
    parts += [
        (_REPO / name).read_text(encoding="utf-8", errors="replace")
        for name in _CODE_FILES
        if (_REPO / name).is_file()
    ]
    return "\n".join(parts)


def unset_env(monkeypatch, *names: str) -> None:
    """Remove each variable from the environment for this test, when it is set.

    Fails when nothing under src/ or scripts/ names the variable any more: the code
    stopped reading it, so clearing it isolates nothing and the test should change
    with the code.
    """
    for name in names:
        assert re.search(rf"\b{re.escape(name)}\b", _code_text()), (
            f"nothing under src/ or scripts/ reads {name}; was it renamed?"
        )
        if name in os.environ:
            monkeypatch.delenv(name)

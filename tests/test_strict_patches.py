"""No patch in the suite may go on passing once what it patches is gone.

`monkeypatch.setattr(..., raising=False)` and `mock.patch(..., create=True)` make a
patch whose target was renamed a silent no-op: the test still passes, and no longer
stands in for anything. The audit of 2026-10-11 counted 44 (code-tests-ci-06); they
were replaced, and this keeps new ones out. To clear an environment variable a shell
may or may not export, use tests.patching.unset_env, which also fails once nothing
reads the variable.
"""

import ast
import os
import re
from pathlib import Path

import pytest

from tests.patching import unset_env

_TESTS = Path(__file__).parent
_SUSPECT = re.compile(r"\braising\s*=\s*False\b|\bcreate\s*=\s*True\b")
_MOCK_PATCHERS = {"patch", "object", "multiple"}


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _is_lenient(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if not isinstance(keyword.value, ast.Constant):
            continue
        if keyword.arg == "raising" and keyword.value.value is False:
            return True
        if (
            keyword.arg == "create"
            and keyword.value.value is True
            and _callee(node) in _MOCK_PATCHERS
        ):
            return True
    return False


def _lenient_patches() -> list[str]:
    found = []
    for path in sorted(_TESTS.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not _SUSPECT.search(text):
            continue
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Call) and _is_lenient(node):
                found.append(f"{path.relative_to(_TESTS.parent)}:{node.lineno}")
    return found


def test_no_patch_tolerates_a_missing_target():
    assert _lenient_patches() == []


def test_the_scan_finds_both_kinds(tmp_path):
    """The scan above must be able to fail: it reads the calls, not the text."""
    sample = (
        "monkeypatch.setattr(mod, 'X', 1, raising=False)\n"
        "monkeypatch.delenv('X', raising=False)\n"
        "mock.patch('a.b', create=True)\n"
        "mock.patch.object(a, 'b', create=True)\n"
        "# raising=False in a comment is not a patch\n"
        "Path(x).mkdir(parents=True)\n"
        "store.create(create=True)\n"
    )
    calls = [n for n in ast.walk(ast.parse(sample)) if isinstance(n, ast.Call)]

    assert sorted(n.lineno for n in calls if _is_lenient(n)) == [1, 2, 3, 4]


def test_unset_env_clears_a_variable_the_code_reads(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")

    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")

    assert "BRAIN_EXTRACT_CALL_SHAPE" not in os.environ


def test_unset_env_accepts_a_variable_that_is_not_set(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    monkeypatch.delenv("BRAIN_EXTRACT_CALL_SHAPE")

    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")


def test_unset_env_fails_on_a_name_nothing_reads(monkeypatch):
    with pytest.raises(AssertionError):
        unset_env(monkeypatch, "BRAIN_NO_SUCH_SETTING_EVER")

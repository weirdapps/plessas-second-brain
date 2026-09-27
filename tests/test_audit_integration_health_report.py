"""The report says why the Calendar row warns (mcp-1 follow-up).

check_calendar turns an empty week ahead into WARN with a note, but build_report
printed notes only for the rows it knew, so the reader saw 'WARN' and no reason.
"""

import importlib.util
from pathlib import Path

HEALTH_CHECK_PATH = Path(__file__).resolve().parent.parent / "scripts" / "health_check.py"


def _hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_calendar_row_prints_its_note():
    hc = _hc()
    row = {
        "name": "Calendar",
        "total": 1082,
        "age": None,
        "status": "WARN",
        "note": "no meeting in the next 7 days",
    }

    text, issues = hc.build_report([row], {}, {}, {}, [])

    line = next(line for line in text.splitlines() if line.strip().startswith("Calendar"))
    assert "(no meeting in the next 7 days)" in line
    assert "Calendar: WARN" in issues

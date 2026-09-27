"""Extraction runs on the engine config.py normalised and checked (docs-3).

config.py strips and lowercases BRAIN_EXTRACT_ENGINE and refuses anything but
claude or gemini. local.py read the variable again, raw, and tests
engine == "claude", so a value of 'Claude ' sent every email down the Gemini
branch while config.py had accepted it.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_extraction_uses_the_engine_as_config_normalised_it(tmp_path):
    env = {**os.environ, "BRAIN_EXTRACT_ENGINE": "Claude ", "BRAIN_DATA_DIR": str(tmp_path)}

    out = subprocess.run(
        [sys.executable, "-c", "from src.extract import local; print(repr(local.DEFAULT_ENGINE))"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    assert out.stdout.strip() == "'claude'"

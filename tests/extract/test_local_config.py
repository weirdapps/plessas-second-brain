"""local.py takes its settings from src.config, like every other module."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_brain_gemini_model_reaches_the_gemini_extractor():
    """local.py pinned its own model, so BRAIN_GEMINI_MODEL changed nothing."""
    out = subprocess.run(
        [sys.executable, "-c", "from src.extract import local; print(local.GEMINI_MODEL)"],
        cwd=ROOT,
        env={**os.environ, "BRAIN_GEMINI_MODEL": "gemini-test-model"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert out.stdout.splitlines()[-1] == "gemini-test-model"

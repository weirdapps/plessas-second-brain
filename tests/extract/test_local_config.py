"""local.py takes its settings from src.config, like every other module."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# Run in a fresh interpreter: the model is read at import, and this process has
# already imported src.config.
_SEND_ONE = """
from unittest.mock import MagicMock
from google import genai
client = MagicMock()
client.models.generate_content.return_value.text = "{}"
genai.Client = lambda **kwargs: client
from src.extract import local
local.extract_one({"message_id": "m1", "subject": "s", "content": "c"}, None, engine="gemini")
print(client.models.generate_content.call_args.kwargs["model"])
"""


def test_brain_gemini_model_reaches_the_gemini_request():
    """local.py pinned its own model, so BRAIN_GEMINI_MODEL changed nothing."""
    out = subprocess.run(
        [sys.executable, "-c", _SEND_ONE],
        cwd=ROOT,
        env={**os.environ, "BRAIN_GEMINI_MODEL": "gemini-test-model"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert out.stdout.splitlines()[-1] == "gemini-test-model"

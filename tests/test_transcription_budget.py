"""A transcription whose thinking used the whole budget is asked again with room to answer.

On 2026-10-07, 38 of 303 content images came back with stop_reason 'max_tokens' and
only a thinking block: the model spent the 1,500-token budget deliberating and never
wrote the text, so the image counted a failed attempt.
"""

import random

import pytest
from PIL import Image

from src.extract.image_vision import TRANSCRIBE_MAX_TOKENS, transcribe_image


class _Thinking:
    """A thinking block: it carries .thinking and no .text."""

    thinking = "weighing the table layout"


class _Text:
    def __init__(self, text):
        self.text = text


class _Reply:
    def __init__(self, stop_reason, *blocks):
        self.stop_reason = stop_reason
        self.content = list(blocks)


def _png(tmp_path):
    path = tmp_path / "image001.png"
    Image.frombytes("RGB", (64, 64), random.Random(1).randbytes(64 * 64 * 3)).save(path)
    return path


def _model(monkeypatch, *replies):
    budgets = []
    queue = list(replies)

    def fake(**kwargs):
        budgets.append(kwargs["max_tokens"])
        return queue.pop(0)

    monkeypatch.setattr("src.extract.claude_extract.complete", fake)
    return budgets


def test_a_reply_cut_short_by_thinking_is_asked_again_with_a_larger_budget(tmp_path, monkeypatch):
    budgets = _model(
        monkeypatch,
        _Reply("max_tokens", _Thinking()),
        _Reply("end_turn", _Thinking(), _Text("Q1 | 1,250")),
    )

    assert transcribe_image(_png(tmp_path)) == "Q1 | 1,250"
    assert budgets[0] == TRANSCRIBE_MAX_TOKENS
    assert len(budgets) == 2 and budgets[1] > budgets[0]


def test_a_complete_reply_is_not_asked_again(tmp_path, monkeypatch):
    budgets = _model(monkeypatch, _Reply("end_turn", _Text("Q1 | 1,250")))

    assert transcribe_image(_png(tmp_path)) == "Q1 | 1,250"
    assert len(budgets) == 1


def test_a_second_reply_still_without_text_fails_the_attempt(tmp_path, monkeypatch):
    budgets = _model(
        monkeypatch,
        _Reply("max_tokens", _Thinking()),
        _Reply("max_tokens", _Thinking()),
    )

    with pytest.raises(ValueError, match="no text block"):
        transcribe_image(_png(tmp_path))
    assert len(budgets) == 2

"""News is not sent to the model: news-reader already wrote the synthesis.

12,249 news items went through the full extraction on the top model, written for
the owner's own mail, and produced 19,533 decisions and 12,948 actions that the
store then hides by default.
"""

import json

import pytest

from src.extract.news_extract import (
    ARTICLE_SUMMARY_CHARS,
    BRIEF_CHARS,
    MAX_TOPICS,
    extract_news,
    is_news,
)

BRIEF = {
    "executive_brief": ["Rates held.", "A bank launched instant payments."],
    "sections": [
        {"category": "macro_rates", "display_name": "Fed Holds, Signals One Cut"},
        {"category": "payments", "display_name": "Instant Payments Arrive"},
        {"display_name": "Uncategorised"},
        {"category": "payments", "display_name": "More Payments"},
    ],
}


def _synthesis(body: str) -> dict:
    return {
        "message_id": "news:synthesis:digest:7",
        "mailbox_name": "News",
        "subject": "[News/digest] morning - 2026-09-24 07:00",
        "content": "Pipeline: digest | Type: morning | Articles: 40\n" + body,
    }


ARTICLE = {
    "message_id": "news:article:0a1b",
    "mailbox_name": "News",
    "subject": "Bank launches instant payments",
    "content": "Opening paragraph. "
    * 80
    + "\n\n---\nURL: https://example.com/a\nPipeline: digest\nRelevance: 80\n"
    "Categories: fintech, payments\nTickers: ACME",
}


def test_only_the_news_mailbox_is_news():
    assert is_news(ARTICLE)
    assert not is_news({"message_id": "m1", "mailbox_name": "Inbox"})
    assert not is_news({"message_id": "m2"})


def test_a_synthesis_takes_its_brief_and_its_section_categories():
    """Display names are written new for each day's news; the category is stable."""
    extraction = extract_news(_synthesis(json.dumps(BRIEF, indent=2)))

    assert extraction["summary"] == "Rates held. A bank launched instant payments."
    assert extraction["topics"] == ["macro rates", "payments", "Uncategorised"]
    assert extraction["message_id"] == "news:synthesis:digest:7"


def test_a_brief_of_text_objects_reads_as_text():
    """news-reader writes its bullets as {text, article_ids} before flattening them."""
    brief = {"executive_brief": [{"text": "ECB holds.", "article_ids": [3]}, {"text": ""}, 7]}

    assert extract_news(_synthesis(json.dumps(brief)))["summary"] == "ECB holds."


def test_nothing_else_is_invented_for_news():
    extraction = extract_news(_synthesis(json.dumps(BRIEF)))

    for field in ("decisions", "action_items", "commitments", "key_facts", "references"):
        assert extraction[field] == []
    assert extraction["people_roles"] == {}
    assert extraction["sentiment"] == "informational"
    assert extraction["urgency"] == "low"


def test_an_article_takes_its_opening_and_its_categories():
    extraction = extract_news(ARTICLE)

    assert extraction["summary"].startswith("Opening paragraph.")
    assert len(extraction["summary"]) <= ARTICLE_SUMMARY_CHARS
    assert extraction["topics"] == ["fintech", "payments"]


def test_a_short_article_leaves_its_footer_out():
    article = {
        **ARTICLE,
        "content": "Short body.\n\n---\nURL: https://example.com/a\nCategories: fintech",
    }

    assert extract_news(article)["summary"] == "Short body."


def test_an_article_with_a_rule_in_its_text_keeps_what_follows_it():
    """The footer is the last separator news_export appends, not the first."""
    article = {
        **ARTICLE,
        "content": "Part one.\n---\nPart two.\n\n---\nURL: https://example.com/a\nCategories: x",
    }

    extraction = extract_news(article)

    assert extraction["summary"] == "Part one.\n---\nPart two."
    assert extraction["topics"] == ["x"]


def test_the_text_is_kept_as_written():
    """Parsing it as JSON ran the parser's clean-up over the words themselves."""
    article = {**ARTICLE, "content": "Rates were [1.5, ] and {a, } ```json code"}

    assert extract_news(article)["summary"] == "Rates were [1.5, ] and {a, } ```json code"


@pytest.mark.parametrize(
    ("body", "summary"),
    [
        pytest.param("plain text brief, not JSON", "plain text brief, not JSON", id="not-json"),
        pytest.param(json.dumps({"executive_brief": "One line."}), "One line.", id="one-string"),
        pytest.param(json.dumps(["a", "b"]), '["a", "b"]', id="json-list"),
    ],
)
def test_a_synthesis_in_another_shape_still_gets_a_summary(body, summary):
    assert extract_news(_synthesis(body))["summary"] == summary


def test_an_empty_brief_falls_back_to_the_synthesis_itself():
    body = json.dumps({"executive_brief": [], "alerts": ["quiet day"]})

    assert extract_news(_synthesis(body))["summary"] == body


def test_a_long_brief_is_capped():
    body = json.dumps({"executive_brief": ["x" * (BRIEF_CHARS * 3)]})

    assert len(extract_news(_synthesis(body))["summary"]) == BRIEF_CHARS


def test_the_fallback_summary_is_capped_too():
    body = json.dumps({"executive_brief": [], "alerts": ["x" * (ARTICLE_SUMMARY_CHARS * 3)]})

    assert len(extract_news(_synthesis(body))["summary"]) == ARTICLE_SUMMARY_CHARS


def test_section_topics_are_capped():
    sections = [{"category": f"topic_{n}"} for n in range(MAX_TOPICS + 5)]

    topics = extract_news(_synthesis(json.dumps({"sections": sections})))["topics"]

    assert len(topics) == MAX_TOPICS


def test_a_pathologically_nested_synthesis_does_not_raise():
    """json.loads raises RecursionError, not ValueError, and one such item would
    have stopped every extraction run at the same place."""
    extraction = extract_news(_synthesis("[" * 100_000))

    assert extraction["summary"] == "[" * ARTICLE_SUMMARY_CHARS

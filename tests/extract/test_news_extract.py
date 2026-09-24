"""News is not sent to the model: news-reader already wrote the synthesis.

12,249 news items went through the full extraction on the top model, written for
the owner's own mail, and produced 19,533 decisions and 12,948 actions that the
store then hides by default.
"""

import json

from src.extract.news_extract import extract_news, is_news

BRIEF = {
    "executive_brief": ["Rates held.", "A bank launched instant payments."],
    "sections": [{"display_name": "Payments"}, {"display_name": "Macro"}, {"category": "x"}],
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


def test_a_synthesis_takes_its_brief_and_its_section_names():
    extraction = extract_news(_synthesis(json.dumps(BRIEF, indent=2)))

    assert extraction["summary"] == "Rates held. A bank launched instant payments."
    assert extraction["topics"] == ["Payments", "Macro"]
    assert extraction["message_id"] == "news:synthesis:digest:7"


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
    assert len(extraction["summary"]) <= 600
    assert "URL:" not in extraction["summary"]
    assert extraction["topics"] == ["fintech", "payments"]


def test_a_synthesis_that_is_not_json_still_gets_a_summary():
    extraction = extract_news(_synthesis("plain text brief, not JSON"))

    assert extraction["summary"] == "plain text brief, not JSON"
    assert extraction["topics"] == []


def test_a_brief_given_as_one_string_is_kept():
    extraction = extract_news(_synthesis(json.dumps({"executive_brief": "One line."})))

    assert extraction["summary"] == "One line."


def test_a_short_article_leaves_its_footer_out():
    article = {
        **ARTICLE,
        "content": "Short body.\n\n---\nURL: https://example.com/a\nCategories: fintech",
    }

    assert extract_news(article)["summary"] == "Short body."

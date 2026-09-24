"""An email the model cannot process stops being offered, and is loaded anyway.

A refused or unparseable email never entered processed_ids, so every run offered
it again, first in line, at up to 18 LLM calls a run, and the loader never
inserted it, not even its raw body. Now, after EMAIL_MAX_ATTEMPTS runs that failed
because of the email itself, it gets a stub extraction: the loader inserts it
with its content, searchable by keyword, and it is not offered again. News skips
the model altogether.
"""

import json

import pytest

from src.extract import local


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run(emails, outcome, workers): one scheduled extraction run over `emails`."""
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    calls: list[str] = []

    def go(emails, outcome, workers=1):
        monkeypatch.setattr(local, "collect_emails", lambda: emails)

        def extract(email, api_key, engine="claude"):
            calls.append(email["message_id"])
            return outcome(email)

        monkeypatch.setattr(local, "extract_inline", extract)
        return local.run_extraction(workers=workers, deadline_s=600.0)

    go.calls = calls
    go.extracted = tmp_path / "extracted"
    go.state = lambda: json.loads((tmp_path / "state.json").read_text())
    return go


def _item_fault(email):
    return email["message_id"], None, False, True


def _extracted(email):
    return email["message_id"], {"summary": "s"}, False, False


@pytest.mark.parametrize("workers", [1, 3])
def test_an_email_that_fails_on_its_own_account_is_loaded_bare_after_the_cap(run, workers):
    emails = [{"message_id": "bad"}]
    for _ in range(local.EMAIL_MAX_ATTEMPTS):
        run(emails, _item_fault, workers)

    stub = json.loads((run.extracted / "bad.json").read_text())
    assert stub["summary"] == ""
    assert stub["decisions"] == []
    assert stub["message_id"] == "bad"
    assert "bad" in run.state()["processed_ids"]
    run.calls.clear()
    run(emails, _item_fault, workers)
    assert run.calls == []


def test_before_the_cap_it_is_offered_again(run):
    emails = [{"message_id": "bad"}]
    for _ in range(local.EMAIL_MAX_ATTEMPTS - 1):
        run(emails, _item_fault)

    assert not (run.extracted / "bad.json").exists()
    assert run.state()["failed_attempts"] == {"bad": local.EMAIL_MAX_ATTEMPTS - 1}


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(lambda e: (e["message_id"], None, True, False), id="quota"),
        pytest.param(lambda e: (e["message_id"], None, False, False), id="service"),
    ],
)
def test_service_failures_never_count_toward_the_cap(run, outcome):
    """An outage must not turn every email it touched into a stub."""
    emails = [{"message_id": "m"}]
    for _ in range(local.EMAIL_MAX_ATTEMPTS + 2):
        run(emails, outcome)

    assert not (run.extracted / "m.json").exists()
    assert run.state().get("failed_attempts", {}) == {}


def test_a_success_clears_the_count(run):
    emails = [{"message_id": "m"}]
    run(emails, _item_fault)

    run(emails, _extracted)

    assert run.state()["failed_attempts"] == {}


@pytest.mark.parametrize("workers", [1, 3])
def test_news_never_reaches_the_model(run, workers):
    emails = [
        {"message_id": "news:article:1", "mailbox_name": "News", "subject": "t", "content": "Body"},
        {"message_id": "mail", "mailbox_name": "Inbox"},
    ]

    result = run(emails, _extracted, workers)

    assert run.calls == ["mail"]
    assert json.loads((run.extracted / "news:article:1.json").read_text())["summary"] == "Body"
    assert result["extracted"] == 2


def test_an_item_fault_is_not_retried(monkeypatch, tmp_path):
    """The same reply comes back for the same input: a retry only spends calls."""
    calls = []

    def unparseable(email, api_key, engine="gemini"):
        calls.append(email["message_id"])
        raise ValueError("Failed to parse JSON")

    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(local, "extract_one", unparseable)
    monkeypatch.setattr(local.time, "sleep", lambda s: None)

    assert local.extract_inline({"message_id": "m"}, None, engine="claude") == (
        "m",
        None,
        False,
        True,
    )
    assert calls == ["m"]


def test_a_service_error_is_retried_and_is_not_the_emails_fault(monkeypatch, tmp_path):
    calls = []

    def dropped(email, api_key, engine="gemini"):
        calls.append(email["message_id"])
        raise ConnectionError("connection reset")

    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(local, "extract_one", dropped)
    monkeypatch.setattr(local.time, "sleep", lambda s: None)

    assert local.extract_inline({"message_id": "m"}, None, engine="claude") == (
        "m",
        None,
        False,
        False,
    )
    assert len(calls) == 3


def test_an_email_staged_twice_counts_once_a_run(run):
    """The cap counts runs, however many times one run meets the email."""
    run([{"message_id": "bad"}, {"message_id": "bad"}], _item_fault)

    assert run.state()["failed_attempts"] == {"bad": 1}

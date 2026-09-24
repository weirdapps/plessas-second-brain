"""An email the model cannot process stops being offered, and is loaded anyway.

A refused or unparseable email never entered processed_ids, so every run offered
it again, first in line, at up to 18 LLM calls a run, and the loader never
inserted it, not even its raw body. Now, after EMAIL_MAX_ATTEMPTS runs in which
it failed while the model worked for other email, it gets a stub extraction: the
loader inserts it with its content, searchable by keyword, and it is not offered
again. A run in which nothing reached the model counts nothing, so a wrong model
id, a missing credential or an outage cannot stub every email it touches. News
skips the model altogether.
"""

import json

import google.auth.exceptions as gauth
import pytest

from src.extract import local


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run(emails, workers): one scheduled extraction run over `emails`.

    Emails whose id starts with "ok" extract; the rest fail with the outcome
    run.failure (countable by default).
    """
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    calls: list[str] = []

    def extract(email, api_key, engine="claude"):
        calls.append(email["message_id"])
        if email["message_id"].startswith("ok"):
            return email["message_id"], {"summary": "s"}, False, False
        return go.failure(email)

    monkeypatch.setattr(local, "extract_inline", extract)

    def go(emails, workers=1):
        monkeypatch.setattr(local, "collect_emails", lambda: emails)
        return local.run_extraction(workers=workers, deadline_s=600.0)

    go.failure = lambda e: (e["message_id"], None, False, True)
    go.calls = calls
    go.extracted = tmp_path / "extracted"
    go.state = lambda: json.loads((tmp_path / "state.json").read_text())
    return go


def _mail(message_id):
    return {"message_id": message_id, "mailbox_name": "Inbox"}


@pytest.mark.parametrize("workers", [1, 3])
def test_an_email_that_keeps_failing_is_loaded_bare_after_the_cap(run, workers):
    for n in range(local.EMAIL_MAX_ATTEMPTS):
        run([_mail("bad"), _mail(f"ok{n}")], workers)

    stub = json.loads((run.extracted / "bad.json").read_text())
    assert {k: v for k, v in stub.items() if k != "message_id"} == {
        k: v for k, v in local._stub_extraction("x").items() if k != "message_id"
    }
    assert stub["summary"] == ""
    assert stub["message_id"] == "bad"
    assert "bad" in run.state()["processed_ids"]
    run.calls.clear()
    run([_mail("bad"), _mail("ok-last")], workers)
    assert run.calls == ["ok-last"]


def test_before_the_cap_it_is_offered_again(run):
    for n in range(local.EMAIL_MAX_ATTEMPTS - 1):
        run([_mail("bad"), _mail(f"ok{n}")])

    assert not (run.extracted / "bad.json").exists()
    assert run.state()["failed_attempts"] == {"bad": local.EMAIL_MAX_ATTEMPTS - 1}


@pytest.mark.parametrize("workers", [1, 3])
def test_a_run_where_nothing_reached_the_model_counts_nothing(run, workers):
    """A retired model id fails every email alike; three hours of it stubbed them all."""
    for _ in range(local.EMAIL_MAX_ATTEMPTS + 2):
        run([_mail("m1"), _mail("m2")], workers)

    assert not run.extracted.exists() or list(run.extracted.iterdir()) == []
    assert run.state()["failed_attempts"] == {}


@pytest.mark.parametrize("workers", [1, 3])
def test_news_is_no_evidence_that_the_model_works(run, workers):
    news = {"message_id": "news:article:1", "mailbox_name": "News", "content": "Body"}
    for n in range(local.EMAIL_MAX_ATTEMPTS + 1):
        run([_mail("bad"), {**news, "message_id": f"news:article:{n}"}], workers)
        assert run.state()["failed_attempts"] == {}

    assert not (run.extracted / "bad.json").exists()


def test_an_email_that_fails_then_extracts_in_one_run_is_not_counted(run):
    """Counting it would leave a stale count, and at the cap write a stub over the
    extraction the run had just saved."""
    seen: list[str] = []

    def first_fails(email):
        seen.append(email["message_id"])
        if seen.count(email["message_id"]) == 1:
            return email["message_id"], None, False, True
        return email["message_id"], {"summary": "real"}, False, False

    run.failure = first_fails
    run([_mail("flaky"), _mail("flaky"), _mail("ok0")])

    assert run.state()["failed_attempts"] == {}
    assert json.loads((run.extracted / "flaky.json").read_text())["summary"] == "real"


def test_quota_failures_never_count(run):
    run.failure = lambda e: (e["message_id"], None, True, False)
    for n in range(local.EMAIL_MAX_ATTEMPTS + 2):
        run([_mail("m"), _mail(f"ok{n}")])

    assert not (run.extracted / "m.json").exists()
    assert run.state()["failed_attempts"] == {}


@pytest.mark.parametrize("workers", [1, 3])
def test_a_success_clears_the_count(run, workers):
    run([_mail("m"), _mail("ok0")], workers)
    assert run.state()["failed_attempts"] == {"m": 1}
    run.failure = lambda e: (e["message_id"], {"summary": "s"}, False, False)

    run([_mail("m")], workers)

    assert run.state()["failed_attempts"] == {}


def test_an_email_staged_twice_counts_once_a_run(run):
    """The cap counts runs, however many times one run meets the email."""
    run([_mail("bad"), _mail("bad"), _mail("ok0")])

    assert run.state()["failed_attempts"] == {"bad": 1}


@pytest.mark.parametrize("workers", [1, 3])
def test_news_never_reaches_the_model(run, workers):
    emails = [
        {"message_id": "news:article:1", "mailbox_name": "News", "subject": "t", "content": "Body"},
        _mail("ok-mail"),
    ]

    result = run(emails, workers)

    assert run.calls == ["ok-mail"]
    assert json.loads((run.extracted / "news:article:1.json").read_text())["summary"] == "Body"
    assert result["extracted"] == 2


def test_news_does_not_hold_the_quota_breaker_open(run):
    """Interleaved with news, a quota outage kept the run calling the exhausted
    model to its deadline, because every news item reset the count."""
    run.failure = lambda e: (e["message_id"], None, True, False)
    emails = []
    for n in range(10):
        emails.append(_mail(f"m{n}"))
        emails.append({"message_id": f"news:article:{n}", "mailbox_name": "News", "content": "x"})

    result = run(emails)

    assert result["quota_paused"] is True
    assert len(run.calls) == local.CONSECUTIVE_FAIL_THRESHOLD


# --- extract_inline: what is retried, and what counts


@pytest.fixture
def inline(monkeypatch, tmp_path):
    """inline(error): extract_inline over one email whose extraction raises `error`."""
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(local.time, "sleep", lambda s: None)
    monkeypatch.setattr("src.extract.vertex_auth.touch_sentinel", lambda: None)
    monkeypatch.setattr(local, "_shutdown", False)
    calls: list[str] = []

    def go(error):
        def fail(email, api_key, engine="gemini"):
            calls.append(email["message_id"])
            raise error

        monkeypatch.setattr(local, "extract_one", fail)
        return local.extract_inline({"message_id": "m"}, None, engine="claude")

    go.calls = calls
    return go


@pytest.mark.parametrize(
    ("error", "calls", "quota", "countable"),
    [
        pytest.param(ValueError("Failed to parse JSON"), 1, False, True, id="unusable-reply"),
        pytest.param(ConnectionError("connection reset"), 3, False, True, id="service"),
        pytest.param(RuntimeError("404 model not found"), 3, False, True, id="configuration"),
        pytest.param(
            gauth.MalformedError("half-written ADC"), 3, False, True, id="auth-valueerror"
        ),
        pytest.param(RuntimeError("429 RESOURCE_EXHAUSTED"), 3, True, False, id="quota"),
        pytest.param(TimeoutError(), 3, False, True, id="timeout"),
    ],
)
def test_only_an_unusable_reply_goes_unretried_and_quota_never_counts(
    inline, error, calls, quota, countable
):
    """A reply that cannot be used comes back the same for the same input; every
    other failure is retried as before. Counting is left to the run, which counts
    nothing when the model worked for no email."""
    assert inline(error) == ("m", None, quota, countable)
    assert len(inline.calls) == calls


def test_an_expired_credential_never_counts_and_stops_the_run(inline):
    assert inline(gauth.RefreshError("invalid_grant")) == ("m", None, False, False)
    assert inline.calls == ["m"]
    assert local._shutdown is True

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
from concurrent.futures import ThreadPoolExecutor

import google.auth.exceptions as gauth
import pytest
from google.genai import errors as genai_errors

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
            return email["message_id"], {"summary": "s"}, False, None
        return go.failure(email)

    monkeypatch.setattr(local, "extract_inline", extract)

    def go(emails, workers=1):
        monkeypatch.setattr(local, "collect_emails", lambda: emails)
        return local.run_extraction(workers=workers, deadline_s=600.0)

    go.failure = lambda e: (e["message_id"], None, False, "fault")
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


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("first", ["fails", "extracts"])
def test_an_email_that_both_fails_and_extracts_in_one_run_is_not_counted(run, workers, first):
    """Staged twice, one copy fails and one extracts, in either order. Counting it
    would leave a stale count, and at the cap write a stub over the extraction
    the run had just saved."""
    import threading

    seen: list[str] = []
    lock = threading.Lock()
    first_done = threading.Event()

    def one_of_each(email):
        with lock:
            seen.append(email["message_id"])
            nth = seen.count(email["message_id"])
        failing = (nth == 1) == (first == "fails")
        if nth == 2:
            # Usually lands after the first call's result, so `first` picks the
            # order; the assertions hold either way.
            first_done.wait(5)
        else:
            first_done.set()
        if failing:
            return email["message_id"], None, False, "fault"
        return email["message_id"], {"summary": "real"}, False, None

    run.failure = one_of_each
    run([_mail("dup"), _mail("dup"), _mail("ok0")], workers)

    assert run.state()["failed_attempts"] == {}
    assert json.loads((run.extracted / "dup.json").read_text())["summary"] == "real"


def test_quota_failures_never_count(run):
    run.failure = lambda e: (e["message_id"], None, True, None)
    for n in range(local.EMAIL_MAX_ATTEMPTS + 2):
        run([_mail("m"), _mail(f"ok{n}")])

    assert not (run.extracted / "m.json").exists()
    assert run.state()["failed_attempts"] == {}


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("kind", ["fault", "timeout"])
def test_a_success_clears_the_count(run, workers, kind):
    counts = {"fault": "failed_attempts", "timeout": "timeout_attempts"}[kind]
    run.failure = lambda e: (e["message_id"], None, False, kind)
    run([_mail("m"), _mail("ok0")], workers)
    assert run.state()[counts] == {"m": 1}
    run.failure = lambda e: (e["message_id"], {"summary": "s"}, False, None)

    run([_mail("m")], workers)

    assert run.state()[counts] == {}


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


@pytest.mark.parametrize("workers", [1, 3])
def test_news_does_not_hold_the_quota_breaker_open(run, workers):
    """Interleaved with news, a quota outage kept the run calling the exhausted
    model to its deadline, because every news item reset the count. The
    concurrent path checks between chunks, so it stops at a chunk boundary."""
    run.failure = lambda e: (e["message_id"], None, True, None)
    emails = []
    for n in range(100):
        emails.append(_mail(f"m{n}"))
        emails.append({"message_id": f"news:article:{n}", "mailbox_name": "News", "content": "x"})

    result = run(emails, workers)

    assert result["quota_paused"] is True
    assert len(run.calls) < 100
    if workers == 1:
        assert len(run.calls) == local.CONSECUTIVE_FAIL_THRESHOLD


# --- extract_inline: what is retried, and what counts


def _sdk_timeout(cause=None):
    """The SDK's own timeout, through the module that already imports the SDK."""
    from src.extract import policy_bridge

    cls = policy_bridge.anthropic.APITimeoutError
    exc = cls.__new__(cls)
    exc.__cause__ = cause
    return exc


def _sdk_status(name, status):
    from src.extract import policy_bridge

    cls = getattr(policy_bridge.anthropic, name)
    exc = cls.__new__(cls)
    exc.status_code = status
    return exc


def _connect_timeout():
    import httpx2

    return httpx2.ConnectTimeout(
        "connect", request=httpx2.Request("POST", "https://example.invalid")
    )


@pytest.fixture
def inline(monkeypatch, tmp_path):
    """inline(error, engine): extract_inline over one email whose extraction raises
    `error`. Run in a worker thread, so the Gemini path sets no SIGALRM here."""
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(local.time, "sleep", lambda s: None)
    monkeypatch.setattr("src.extract.vertex_auth.touch_sentinel", lambda: None)
    monkeypatch.setattr(local, "_shutdown", False)
    calls: list[str] = []

    def go(error, engine="claude"):
        def fail(email, api_key, engine="gemini"):
            calls.append(email["message_id"])
            raise error

        monkeypatch.setattr(local, "extract_one", fail)
        with ThreadPoolExecutor(1) as pool:
            return pool.submit(local.extract_inline, {"message_id": "m"}, None, 3, engine).result()

    go.calls = calls
    return go


@pytest.mark.parametrize("engine", ["claude", "gemini"])
@pytest.mark.parametrize(
    ("error", "gemini_calls", "quota", "failure"),
    [
        pytest.param(ValueError("Failed to parse JSON"), 1, False, "fault", id="unusable-reply"),
        pytest.param(RuntimeError("400 prompt is too long"), 3, False, "fault", id="rejected"),
        pytest.param(
            gauth.MalformedError("half-written ADC"), 3, False, "fault", id="auth-valueerror"
        ),
        pytest.param(ConnectionError("connection reset"), 3, False, None, id="service"),
        pytest.param(
            genai_errors.ServerError(503, {"error": {"status": "UNAVAILABLE"}}),
            3,
            False,
            None,
            id="gemini-503",
        ),
        pytest.param(TimeoutError(), 3, False, "timeout", id="timeout"),
        pytest.param(_sdk_timeout(), 3, False, "timeout", id="sdk-timeout"),
        pytest.param(_sdk_timeout(_connect_timeout()), 3, False, None, id="connect-timeout"),
        pytest.param(_sdk_status("APIStatusError", 408), 3, False, "timeout", id="408"),
        pytest.param(_sdk_status("InternalServerError", 504), 3, False, "timeout", id="504"),
        pytest.param(
            genai_errors.ServerError(504, {"error": {"status": "DEADLINE_EXCEEDED"}}),
            3,
            False,
            "timeout",
            id="gemini-504",
        ),
        pytest.param(RuntimeError("429 RESOURCE_EXHAUSTED"), 3, True, None, id="quota"),
    ],
)
def test_what_is_retried_and_what_counts(inline, engine, error, gemini_calls, quota, failure):
    """The email's own failure is a fault, a timeout counts slower, a transient one
    is the service's.
    Claude is tried once here, its retries being the policy's inside complete();
    Gemini, which has no policy, is retried unless the reply was unusable. The run
    still counts nothing when the model worked for no email."""
    assert inline(error, engine) == ("m", None, quota, failure)
    assert len(inline.calls) == (1 if engine == "claude" else gemini_calls)


def test_an_expired_credential_never_counts_and_stops_the_run(inline):
    assert inline(gauth.RefreshError("invalid_grant")) == ("m", None, False, None)
    assert inline.calls == ["m"]
    assert local._shutdown is True


def test_a_reply_whose_error_mentions_429_is_a_fault_not_quota(inline):
    """The parser's message carries a column number, and one of them was 429."""
    error = ValueError("Failed to parse JSON: Expecting ',' delimiter: line 1 column 4291")

    assert inline(error) == ("m", None, False, "fault")


def test_an_email_that_keeps_timing_out_is_retired_on_the_longer_cap(run):
    """Too big to answer in time, it timed out every run and was offered forever;
    a slow spell times out now and then, so timeouts count ten times slower."""
    run.failure = lambda e: (e["message_id"], None, False, "timeout")
    for n in range(local.EMAIL_MAX_TIMEOUTS - 1):
        run([_mail("huge"), _mail(f"ok{n}")])
    assert run.state()["timeout_attempts"] == {"huge": local.EMAIL_MAX_TIMEOUTS - 1}
    assert not (run.extracted / "huge.json").exists()

    run([_mail("huge"), _mail("ok-last")])

    assert json.loads((run.extracted / "huge.json").read_text())["summary"] == ""
    assert run.state()["timeout_attempts"] == {}


@pytest.mark.parametrize("order", [["timeout", "fault"], ["fault", "timeout"]])
def test_a_fault_outranks_a_timeout_in_the_same_run(run, order):
    kinds = iter(order)
    run.failure = lambda e: (e["message_id"], None, False, next(kinds))

    run([_mail("both"), _mail("both"), _mail("ok0")])

    assert run.state()["failed_attempts"] == {"both": 1}
    assert run.state()["timeout_attempts"] == {}


@pytest.mark.parametrize("api_key", ["k", None])
def test_the_gemini_client_gives_up_on_a_call_in_time(monkeypatch, api_key):
    """Worker threads have no SIGALRM, and the client's own default is no timeout
    at all: an email too big to answer hung its worker, never counted."""
    from google import genai

    made: dict = {}

    class Client:
        def __init__(self, **kwargs):
            made.update(kwargs)
            self.models = self

        def generate_content(self, **kwargs):
            return type("Reply", (), {"text": '{"summary": "s"}'})()

    monkeypatch.setattr(genai, "Client", Client)

    out = local.extract_one(_mail("m"), api_key, engine="gemini")

    assert out["summary"] == "s"
    assert made["http_options"].timeout == local.CALL_TIMEOUT * 1000
    assert made.get("api_key") == api_key

"""A 4xx other than 429 is never quota, whatever digits its message holds
(extract-core-5).

A deterministic 400 whose text held '429' (a token count such as 214290, a
request id) was classed as RATE_LIMIT by the string widener, and the
conversation path also matched it through _parse_retry_delay's bare substring
test. The conversation came back as quota, so it never counted toward
CONVERSATION_MAX_ATTEMPTS and was retried every hour as a poison item.
"""

import pytest

from src.extract import local
from src.extract.local import _should_quota_pause
from src.extract.policy_bridge import classify_exception, is_transient
from src.llm_policy import Outcome

TOO_LONG = "prompt is too long: 214290 tokens > 200000 maximum"


def _status_error(status, message):
    """What the Vertex client makes of a status, built through the module that
    already imports the SDK, as tests/extract/test_local_quota.py does."""
    import httpx2

    from src.extract import policy_bridge

    client = policy_bridge.anthropic.AnthropicVertex(region="eu", project_id="p", access_token="t")
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://example.invalid"))
    return client._make_status_error(message, body=None, response=response)


def test_a_400_holding_429_in_its_text_is_an_api_error():
    assert classify_exception(_status_error(400, TOO_LONG), None) is Outcome.API_ERROR


@pytest.mark.parametrize("status", [400, 404, 408, 409, 413, 422])
def test_no_4xx_but_429_is_a_rate_limit(status):
    exc = _status_error(status, f"request req_vrtx_429abc failed: {TOO_LONG}")
    assert classify_exception(exc, None) is not Outcome.RATE_LIMIT
    assert _should_quota_pause(exc) is False


def test_a_real_429_is_still_a_rate_limit():
    exc = _status_error(429, "RESOURCE_EXHAUSTED")
    assert classify_exception(exc, None) is Outcome.RATE_LIMIT
    assert _should_quota_pause(exc) is True


def test_401_and_403_are_still_auth():
    for status in (401, 403):
        assert classify_exception(_status_error(status, "x"), None) is (
            Outcome.AUTH_REAUTH_REQUIRED
        )


def test_408_and_409_are_still_worth_offering_again():
    assert is_transient(_status_error(408, "timeout"))
    assert is_transient(_status_error(409, "conflict"))


@pytest.fixture
def conversation_raising(monkeypatch, tmp_path):
    from src.extract import claude_extract

    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")

    def _set(exc):
        def _extract(conversation):
            raise exc

        monkeypatch.setattr(claude_extract, "extract_conversation", _extract)

    return _set


def test_a_too_long_conversation_counts_toward_its_attempt_cap(conversation_raising):
    conversation_raising(_status_error(400, TOO_LONG))

    result = local.extract_conversation_inline({"session_id": "s1"})

    assert result == ("s1", None, False, local.FAULT)


def test_a_conversation_quota_error_is_still_quota(conversation_raising):
    conversation_raising(_status_error(429, "RESOURCE_EXHAUSTED, retry in 0h5m"))

    assert local.extract_conversation_inline({"session_id": "s1"}) == ("s1", None, True, None)

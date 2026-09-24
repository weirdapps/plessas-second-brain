# tests/extract/test_policy_bridge.py
import anthropic
import google.auth.exceptions as gauth

from src.extract.policy_bridge import classify_exception
from src.llm_policy import Outcome


def test_a_local_refresh_failure_is_auth():
    # The one that actually happens: raised before any request is issued, so it
    # never reaches the Anthropic SDK's exception hierarchy.
    # Message is deliberately chosen to NOT match _AUTH_PATTERNS, so only the
    # isinstance(exc, gauth.RefreshError) type check can save this test.
    exc = gauth.RefreshError("token expired")
    assert classify_exception(exc, None) is Outcome.AUTH_REAUTH_REQUIRED


def test_the_string_widener_catches_a_wrapped_auth_error():
    # A non-gauth, non-anthropic exception whose message matches _AUTH_PATTERNS;
    # the type checks above all miss it, so only is_vertex_auth_error() saves it.
    exc = RuntimeError("reauthentication is needed")
    assert classify_exception(exc, None) is Outcome.AUTH_REAUTH_REQUIRED


def test_a_server_side_rejection_is_also_auth():
    exc = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
    assert classify_exception(exc, None) is Outcome.AUTH_REAUTH_REQUIRED


def test_a_permission_denied_is_auth():
    exc = anthropic.PermissionDeniedError.__new__(anthropic.PermissionDeniedError)
    assert classify_exception(exc, None) is Outcome.AUTH_REAUTH_REQUIRED


def test_a_rate_limit_is_not_auth():
    exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
    assert classify_exception(exc, None) is Outcome.RATE_LIMIT


def test_an_overloaded_error_is_a_rate_limit():
    # OverloadedError (529) shares the rate-limit retry posture (cap 3, 60s base)
    # but is NOT a subclass of RateLimitError; it needs its own isinstance branch.
    exc = anthropic.OverloadedError.__new__(anthropic.OverloadedError)
    assert classify_exception(exc, None) is Outcome.RATE_LIMIT


def test_a_connection_error_is_an_api_error():
    exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
    assert classify_exception(exc, None) is Outcome.API_ERROR


def test_a_refusal_response_is_a_refusal():
    resp = type("R", (), {"stop_reason": "refusal"})()
    assert classify_exception(None, resp) is Outcome.REFUSAL


def test_a_max_tokens_stop_is_truncated_not_retryable():
    # Retrying with identical parameters re-truncates, so this must terminate.
    resp = type("R", (), {"stop_reason": "max_tokens"})()
    assert classify_exception(None, resp) is Outcome.TRUNCATED


def test_a_normal_response_is_ok():
    resp = type("R", (), {"stop_reason": "end_turn"})()
    assert classify_exception(None, resp) is Outcome.OK


def test_no_exception_and_no_response_is_empty():
    # Both None means the caller received nothing; one retry is warranted.
    # Previously returned OK, which would fool decide() into treating a missing
    # extraction as a success.
    assert classify_exception(None, None) is Outcome.EMPTY


def test_a_bare_quota_string_is_rate_limit_not_api_error():
    # A plain exception from a non-Anthropic Vertex path (e.g. Gemini) carrying
    # the 429 or RESOURCE_EXHAUSTED marker gets the rate-limit retry posture
    # (cap 3, 60s base) rather than the API_ERROR cap-2 fallback.
    exc = Exception("429 RESOURCE_EXHAUSTED")
    assert classify_exception(exc, None) is Outcome.RATE_LIMIT


def test_auth_type_check_beats_the_rate_limit_string_widener():
    # An auth exception whose message matches the quota pattern must classify as
    # auth, not rate-limit: the isinstance check runs before either string widener.
    exc = gauth.RefreshError("429 RESOURCE_EXHAUSTED")
    assert classify_exception(exc, None) is Outcome.AUTH_REAUTH_REQUIRED


def _status_error(cls, status):
    exc = cls.__new__(cls)
    exc.status_code = status
    return exc


def test_is_transient_knows_the_service_failures_the_sdk_raises():
    """What call_with_policy re-raises for a real 5xx, a dropped connection or a
    token refresh that could not reach Google, not only the builtin types."""
    from src.extract.policy_bridge import is_transient

    cases = [
        _status_error(anthropic.InternalServerError, 500),
        _status_error(anthropic.APIStatusError, 503),
        anthropic.APIConnectionError.__new__(anthropic.APIConnectionError),
        anthropic.APITimeoutError.__new__(anthropic.APITimeoutError),
        anthropic.RateLimitError.__new__(anthropic.RateLimitError),
        gauth.TransportError("token endpoint unreachable"),
        gauth.TimeoutError("token refresh timed out"),
        ConnectionError("reset by peer"),
    ]

    assert [is_transient(e) for e in cases] == [True] * len(cases)


def test_is_transient_knows_the_gemini_engines_service_failures():
    """The Gemini engine has no policy in front of it, and raises its SDK's own
    5xx and httpx's transport errors unwrapped."""
    import httpx
    from google.genai import errors as genai_errors

    from src.extract.policy_bridge import is_transient

    assert is_transient(genai_errors.ServerError(503, {"error": {"status": "UNAVAILABLE"}}))
    assert is_transient(httpx.ConnectError("connection refused"))
    assert not is_transient(genai_errors.ClientError(400, {"error": {"status": "INVALID"}}))
    for code in (408, 409, 499):
        assert is_transient(genai_errors.ClientError(code, {"error": {}})), code


def test_is_transient_knows_the_statuses_the_sdk_itself_retries():
    from src.extract.policy_bridge import is_transient

    assert is_transient(_status_error(anthropic.APIStatusError, 408))
    assert is_transient(_status_error(anthropic.ConflictError, 409))
    assert not is_transient(_status_error(anthropic.NotFoundError, 404))


def test_is_transient_leaves_an_unusable_reply_permanent():
    from src.extract.policy_bridge import is_transient

    assert not is_transient(ValueError("calendar extraction response is not JSON"))
    assert not is_transient(_status_error(anthropic.BadRequestError, 400))
    assert not is_transient(gauth.RefreshError("invalid_grant"))


def test_reset_client_cache_is_registered_as_a_post_reauth_callback():
    from src import llm_policy
    from src.extract import (
        claude_extract,
        policy_bridge,  # noqa: F401 — side-effect: registers reset_client_cache
    )

    count = llm_policy._POST_REAUTH.count(claude_extract.reset_client_cache)
    assert count == 1, (
        f"reset_client_cache registered {count} time(s); expected exactly 1. "
        "A count of 2 means policy_bridge was imported under two different module "
        "names (e.g. both 'src.extract.policy_bridge' and 'extract.policy_bridge') "
        "causing the module-level register_post_reauth call to execute twice."
    )


def test_an_unusable_reply_is_never_transient_whatever_its_message_says():
    """A parser's column number read as a 429: calendar sync kept the event
    pending and extracted it again every run."""
    from src.extract.policy_bridge import is_transient

    for message in (
        "Failed to parse JSON: Expecting ',' delimiter: line 1 column 4291 (char 4290)",
        "Failed to parse JSON: Unterminated string starting at: line 1 column 529",
        "Failed to parse JSON: Expecting value: line 408 column 1 timeout",
    ):
        assert not is_transient(ValueError(message)), message


def _sdk_timeout(cause=None):
    exc = anthropic.APITimeoutError.__new__(anthropic.APITimeoutError)
    exc.__cause__ = cause
    return exc


def test_a_timeout_of_the_request_is_told_apart_from_one_reaching_the_service():
    """A request that runs out of time will again; a connection that could not be
    made, or waited for a pooled one, says nothing about the request."""
    import httpx
    import httpx2
    from google.genai import errors as genai_errors

    from src.extract.policy_bridge import is_item_timeout

    request = httpx.Request("POST", "https://example.invalid")
    request2 = httpx2.Request("POST", "https://example.invalid")
    of_the_request = [
        TimeoutError(),
        _sdk_timeout(),
        _sdk_timeout(httpx2.ReadTimeout("read", request=request2)),
        httpx.ReadTimeout("read", request=request),
        _status_error(anthropic.APIStatusError, 408),
        _status_error(anthropic.InternalServerError, 504),
        genai_errors.ClientError(408, {"error": {}}),
        genai_errors.ServerError(504, {"error": {"status": "DEADLINE_EXCEEDED"}}),
    ]
    reaching_the_service = [
        _sdk_timeout(httpx2.ConnectTimeout("connect", request=request2)),
        _sdk_timeout(httpx2.PoolTimeout("pool", request=request2)),
        _sdk_timeout(httpx2.WriteTimeout("write", request=request2)),
        httpx.ConnectTimeout("connect", request=request),
        httpx.PoolTimeout("pool", request=request),
        httpx.WriteTimeout("write", request=request),
    ]
    not_timeouts = [
        _status_error(anthropic.InternalServerError, 500),
        _status_error(anthropic.RateLimitError, 429),
        genai_errors.ServerError(503, {"error": {"status": "UNAVAILABLE"}}),
        ValueError("Failed to parse JSON"),
        gauth.TimeoutError("token refresh timed out"),
    ]

    assert [is_item_timeout(e) for e in of_the_request] == [True] * len(of_the_request)
    others = reaching_the_service + not_timeouts
    assert [is_item_timeout(e) for e in others] == [False] * len(others)
    # Still worth offering again, just not the request's doing.
    assert all(is_transient_(e) for e in reaching_the_service)


def is_transient_(exc):
    from src.extract.policy_bridge import is_transient

    return is_transient(exc)


def _vertex_error(status):
    """What the Vertex client itself makes of a status, not a hand-built class."""
    import httpx2

    client = anthropic.AnthropicVertex(region="eu", project_id="p", access_token="t")
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://example.invalid"))
    return client._make_status_error("status", body=None, response=response)


def test_a_vertex_overload_is_rate_limited_as_the_direct_apis_is():
    """The Vertex client has no class for 529: an overload arrived as a plain
    InternalServerError, and never slowed anything down."""
    exc = _vertex_error(529)

    assert type(exc) is anthropic.InternalServerError
    assert classify_exception(exc, None) is Outcome.RATE_LIMIT


def test_what_the_vertex_client_makes_of_a_504_is_the_requests_timeout():
    from src.extract.policy_bridge import is_item_timeout

    assert is_item_timeout(_vertex_error(504))
    assert not is_item_timeout(_vertex_error(503))


def test_a_gateways_504_on_the_way_to_gemini_is_not_the_requests_timeout():
    """Gemini names its own deadline; an HTML 504 from a proxy is the network."""
    import httpx
    from google.genai import errors as genai_errors

    from src.extract.policy_bridge import is_item_timeout

    def raised(response):
        try:
            genai_errors.APIError.raise_for_response(response)
        except genai_errors.APIError as e:
            return e
        raise AssertionError("no error raised")

    request = httpx.Request("POST", "https://example.invalid")
    gateway = raised(httpx.Response(504, text="<html>gateway</html>", request=request))
    deadline = raised(
        httpx.Response(
            504,
            json={"error": {"code": 504, "status": "DEADLINE_EXCEEDED", "message": "late"}},
            request=request,
        )
    )

    assert not is_item_timeout(gateway)
    assert is_item_timeout(deadline)

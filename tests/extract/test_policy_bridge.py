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

"""A retryable google-auth RefreshError is an outage, not an expired credential (llm-2).

google-auth retries a token-endpoint 500/503/504/408/429 itself, then raises
RefreshError(retryable=True). classify_exception called every RefreshError
auth, so on the producer a short-budget job wrote the gcloud reauth sentinel and
stopped, and every gated job waited for sb-auth-watch, over a transport failure.
"""

import google.auth.exceptions as gauth

from src.extract.policy_bridge import classify_exception, is_transient
from src.llm_policy import Outcome


def _retryable():
    return gauth.RefreshError(
        "server_error: backend unavailable", {"error": "server_error"}, retryable=True
    )


def test_a_retryable_refresh_error_is_an_api_error():
    assert classify_exception(_retryable(), None) is Outcome.API_ERROR


def test_a_retryable_refresh_error_is_transient():
    assert is_transient(_retryable()) is True


def test_a_retryable_refresh_error_matching_the_auth_patterns_is_still_an_api_error():
    """The type check runs before the string widener, which knows 'refresherror'."""
    exc = gauth.RefreshError("RefreshError: temporarily_unavailable", retryable=True)
    assert classify_exception(exc, None) is Outcome.API_ERROR


def test_a_plain_refresh_error_is_still_auth_and_not_transient():
    exc = gauth.RefreshError("Reauthentication is needed")
    assert classify_exception(exc, None) is Outcome.AUTH_REAUTH_REQUIRED
    assert is_transient(exc) is False


def test_a_refresh_error_marked_not_retryable_is_still_auth():
    exc = gauth.RefreshError("invalid_grant", retryable=False)
    assert classify_exception(exc, None) is Outcome.AUTH_REAUTH_REQUIRED

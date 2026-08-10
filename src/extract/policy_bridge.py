"""Maps this repo's SDK failures onto the shared policy, and wires the one
callback without which a re-auth here cannot work.

Two facts shape this file, both measured rather than assumed:

  * The failure that actually occurs is google.auth.exceptions.RefreshError,
    raised while refreshing the local credential and therefore before any HTTP
    request exists. It never enters the Anthropic SDK's exception hierarchy, so
    catching only anthropic.AuthenticationError would miss every real case.
  * The AnthropicVertex client is cached on purpose: building one forks
    `gcloud config get project` and costs about two seconds. That means a
    successful re-auth changes nothing until the cache is dropped, which is why
    reset_client_cache is registered below.
"""

import anthropic
import google.auth.exceptions as gauth

from src.extract.claude_extract import reset_client_cache
from src.extract.vertex_auth import is_vertex_auth_error
from src.llm_policy import Outcome, register_post_reauth


def classify_exception(exc: BaseException | None, response: object | None) -> Outcome:
    """Map one SDK outcome to a policy Outcome. Types first, strings second."""
    if exc is not None:
        if isinstance(exc, gauth.RefreshError):
            return Outcome.AUTH_REAUTH_REQUIRED
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return Outcome.AUTH_REAUTH_REQUIRED
        if isinstance(exc, anthropic.RateLimitError):
            return Outcome.RATE_LIMIT
        if isinstance(exc, anthropic.APITimeoutError):
            return Outcome.TIMEOUT
        # Secondary widening. Kept because it already catches the case that
        # happens, and because a wrapped or re-raised error can lose its type.
        if is_vertex_auth_error(exc):
            return Outcome.AUTH_REAUTH_REQUIRED
        return Outcome.API_ERROR

    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        return Outcome.REFUSAL
    if stop == "max_tokens":
        return Outcome.TRUNCATED
    return Outcome.OK


register_post_reauth(reset_client_cache)

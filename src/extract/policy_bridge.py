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

_RATE_LIMIT_PATTERNS = ("429", "resource_exhausted")


def _is_rate_limit_error(err: object) -> bool:
    """Secondary string widener for quota / rate-limit errors.

    Mirrors ``vertex_auth.is_vertex_auth_error`` for auth: the SDK type checks
    handle ``anthropic.RateLimitError`` and ``anthropic.OverloadedError``; this
    catches plain exceptions from non-Anthropic Vertex paths (e.g. Gemini) that
    carry the HTTP 429 or gRPC RESOURCE_EXHAUSTED status in their message.

    MUST run after all type checks and after the auth widener so that an auth
    exception whose message happens to contain "429" is classified as auth, not
    as rate-limit.
    """
    msg = str(err).lower()
    return any(p in msg for p in _RATE_LIMIT_PATTERNS)


def classify_exception(exc: BaseException | None, response: object | None) -> Outcome:
    """Map one SDK outcome to a policy Outcome. Types first, strings second."""
    if exc is not None:
        if isinstance(exc, gauth.RefreshError):
            return Outcome.AUTH_REAUTH_REQUIRED
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return Outcome.AUTH_REAUTH_REQUIRED
        if isinstance(exc, anthropic.RateLimitError | anthropic.OverloadedError):
            return Outcome.RATE_LIMIT
        if isinstance(exc, anthropic.APITimeoutError):
            return Outcome.TIMEOUT
        # Secondary wideners — type checks always run first; strings catch only
        # wrapped or re-raised exceptions that have lost their original type.
        # Ordering: auth widener before rate-limit widener so an auth exception
        # whose message contains "429" is classified as auth, not rate-limit.
        if is_vertex_auth_error(exc):
            return Outcome.AUTH_REAUTH_REQUIRED
        if _is_rate_limit_error(exc):
            return Outcome.RATE_LIMIT
        return Outcome.API_ERROR

    if response is None:
        return Outcome.EMPTY

    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        return Outcome.REFUSAL
    if stop == "max_tokens":
        return Outcome.TRUNCATED
    return Outcome.OK


register_post_reauth(reset_client_cache)

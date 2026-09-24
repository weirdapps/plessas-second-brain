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
import httpx
from google.genai import errors as genai_errors

from src.extract.claude_extract import reset_client_cache
from src.extract.vertex_auth import is_vertex_auth_error
from src.llm_policy import Outcome, register_post_reauth

# Timeouts of the network rather than of the request (see is_item_timeout):
# connecting, waiting for a pooled connection, sending. httpx2 is the Anthropic
# SDK's transport, a fork of httpx.
try:
    import httpx2

    _NETWORK_TIMEOUTS: tuple[type[BaseException], ...] = (
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
        httpx.WriteTimeout,
        httpx2.ConnectTimeout,
        httpx2.PoolTimeout,
        httpx2.WriteTimeout,
    )
except ImportError:  # an SDK back on plain httpx
    _NETWORK_TIMEOUTS = (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout)

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


def is_transient(exc: BaseException) -> bool:
    """A failure of the service rather than of the item, so worth offering again.

    call_with_policy re-raises the SDK's last exception when it gives up, so a
    quota error, a timeout, a dropped connection or a 5xx reaches its caller as
    itself. A reply the caller cannot use (unparseable, truncated) raises
    ValueError there, and trying again would get the same reply.
    """
    # Whatever its message says: a parser's column number can read as a '429'.
    # google-auth's own ValueErrors are credentials, and are judged below.
    if isinstance(exc, ValueError) and not isinstance(exc, gauth.GoogleAuthError):
        return False
    if classify_exception(exc, None) in (Outcome.RATE_LIMIT, Outcome.TIMEOUT):
        return True
    if isinstance(exc, anthropic.APIConnectionError | anthropic.InternalServerError):
        return True
    if isinstance(exc, anthropic.APIStatusError) and getattr(exc, "status_code", 0) >= 500:
        return True
    # AnthropicVertex refreshes its Google token outside the SDK's own error
    # wrapping, so a network drop there arrives as google-auth's own types.
    if isinstance(exc, gauth.TransportError | gauth.TimeoutError):
        return True
    # The Gemini engine raises its SDK's own 5xx, and httpx's transport errors
    # unwrapped.
    if isinstance(exc, genai_errors.ServerError | httpx.TransportError):
        return True
    # Worth offering again, below the 5xx line: a request timeout, a conflict, a
    # client-closed request. Anthropic's SDK retries 408 and 409 itself; the
    # Gemini client retries nothing unless it is told to.
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code in (408, 409):
        return True
    if isinstance(exc, genai_errors.APIError) and exc.code in (408, 409, 499):
        return True
    return isinstance(exc, ConnectionError | TimeoutError)


def is_overload(exc: BaseException) -> bool:
    """The service is overloaded: the direct API's OverloadedError, or the 529 the
    Vertex client raises as a plain InternalServerError, having no class for it.

    The extraction loop treats it as quota (local._should_quota_pause). The retry
    policy keeps a Vertex 529 on the API-error budget: the rate-limit backoff
    (60, 120, 240 s) held a call already running well past the sync's slice.
    """
    return isinstance(exc, anthropic.OverloadedError) or (
        isinstance(exc, anthropic.APIStatusError) and getattr(exc, "status_code", 0) == 529
    )


def is_item_timeout(exc: BaseException) -> bool:
    """The request ran out of time, as the same request is likely to again.

    On either side: the client waiting for the reply, or the service giving up
    on it with a 408 or a 504. The Vertex client reports every 504 as
    DeadlineExceededError, whatever answered; Gemini names its own deadline
    (DEADLINE_EXCEEDED), so a gateway's 504 on the way there is not counted.
    Nor is a timeout of the network: connecting, waiting for a pooled
    connection, or sending a request body, which is at most ~50K characters
    here. The Anthropic SDK wraps every timeout of its transport in
    APITimeoutError, so the cause tells them apart. The Vertex mTLS transport
    (requests) is not used here, and its timeouts are not classified.
    """
    if isinstance(exc, _NETWORK_TIMEOUTS) or isinstance(exc.__cause__, _NETWORK_TIMEOUTS):
        return False
    if classify_exception(exc, None) is Outcome.TIMEOUT:
        return True
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return True
    if isinstance(exc, anthropic.APIStatusError) and getattr(exc, "status_code", 0) in (408, 504):
        return True
    if isinstance(exc, genai_errors.APIError):
        return exc.code == 408 or (exc.code == 504 and exc.status == "DEADLINE_EXCEEDED")
    return False


register_post_reauth(reset_client_cache)

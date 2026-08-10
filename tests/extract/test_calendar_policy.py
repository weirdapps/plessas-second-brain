"""Auth-retry policy tests for calendar_extractor.

Before the fix: a single auth error in extract_event escapes the function
and aborts the entire cmd_calendar_sync run — every later event is lost.
After the fix: call_with_policy retries after reauth so one bad event does
not kill the run.
"""

from unittest.mock import patch

import google.auth.exceptions as gauth

from src.extract import calendar_extractor, claude_extract
from src.llm_policy import ReauthResult

# 60 chars > the 50-char minimum that triggers an LLM call in extract_event.
_LONG_BODY = "A" * 60


def test_one_bad_event_does_not_kill_the_whole_sync(monkeypatch):
    """One auth error triggers reauth; the second call succeeds.

    Before the fix: RefreshError escapes from extract_event (no retry);
    cmd_calendar_sync has no per-event guard, so the whole run aborts and
    every later event is lost.
    After the fix: call_with_policy retries after reauth and returns a result.

    running_on_linux is pinned False so decide() chooses REAUTH_RETRY (macOS
    path) and does not give up immediately, as it would on a Linux VPS where
    UNRECOVERABLE_AUTH fires on the first auth failure.

    Mutation checks:
    - Remove call_with_policy (bare create): RefreshError propagates,
      assert result is not None fails (exception, not a dict).
    - Remove the retry (loop exits after 1): len(calls) == 1, not 2.
    """
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise gauth.RefreshError("invalid_grant: Bad Request")
            return type("R", (), {
                "stop_reason": "end_turn",
                "content": [type("C", (), {"text": "{}"})()],
            })()

    fake = type("Client", (), {"messages": FakeMessages()})()
    # Patch _get_client_and_model at calendar_extractor's module level (it is a
    # module-level import there), so _do_call picks up the fake client on each attempt.
    monkeypatch.setattr(calendar_extractor, "_get_client_and_model", lambda: (fake, "m"))
    # Pin the platform so decide() uses the macOS budget (REAUTH_RETRY, not UNRECOVERABLE_AUTH).
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED):
        result = calendar_extractor.extract_event({"id": "e1", "subject": "s"}, body=_LONG_BODY)

    assert result is not None
    assert len(calls) == 2

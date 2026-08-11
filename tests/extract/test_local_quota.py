import google.auth.exceptions as gauth

from src.extract.local import _should_quota_pause


def test_a_quota_error_still_pauses():
    assert _should_quota_pause(Exception("429 RESOURCE_EXHAUSTED")) is True


def test_an_auth_error_does_not_trigger_an_hour_of_pointless_sleep():
    # The credential is still expired when the sleep ends, so the pause buys
    # nothing; and the sentinel that would summon a fix is never written.
    assert _should_quota_pause(gauth.RefreshError("invalid_grant")) is False


def test_an_auth_error_whose_message_matches_the_quota_pattern_still_does_not_pause():
    # Discriminator: _parse_retry_delay would see "429" in the message and return
    # non-None, so a bare string check alone would produce True here.  Only the
    # type-first routing through classify_exception produces False.
    assert _should_quota_pause(gauth.RefreshError("429 RESOURCE_EXHAUSTED")) is False

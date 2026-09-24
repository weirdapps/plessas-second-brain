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


def _vertex_error(status):
    """What the Vertex client itself makes of a status, through the module that
    already imports the SDK."""
    import httpx2

    from src.extract import policy_bridge

    client = policy_bridge.anthropic.AnthropicVertex(region="eu", project_id="p", access_token="t")
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://example.invalid"))
    return client._make_status_error("status", body=None, response=response)


def test_a_vertex_overload_pauses_like_quota():
    """Five in a row end the run, as the direct API's OverloadedError always did;
    the Vertex client raised it as a plain 5xx, and the loop kept going."""
    assert _should_quota_pause(_vertex_error(529)) is True
    assert _should_quota_pause(_vertex_error(500)) is False


def test_extract_inline_reports_a_vertex_overload_as_quota(monkeypatch, tmp_path):
    from src.extract import local

    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")

    def overloaded(email, api_key, engine="gemini"):
        raise _vertex_error(529)

    monkeypatch.setattr(local, "extract_one", overloaded)

    assert local.extract_inline({"message_id": "m"}, None, 3, "claude") == ("m", None, True, None)

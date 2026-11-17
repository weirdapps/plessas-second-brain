"""A gcloud refusal that clears within seconds must not raise needs_gcloud_reauth.

On the VPS between 2026-11-12 and 11-17 gcloud refused "reauth is required" for
seconds at a time on seven days, while gcloud-refresh.sh logged "ADC OK (RAPT OK)"
at every slot either side. Each blip raised the sentinel, which only auth-watch
clears, so Vertex jobs skipped for up to an hour over a credential that was fine.
"""

from __future__ import annotations

from src.extract import vertex_auth

# Captured at import, before conftest's autouse fixture stands it in for every test.
_REAL_CONFIRM = vertex_auth._refusal_confirmed


def _arm(monkeypatch, tmp_path, answers):
    """Real _refusal_confirmed, fake gcloud answering `answers` in turn, no sleeping."""
    fake = tmp_path / "needs_gcloud_reauth"
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", fake)
    monkeypatch.setattr(vertex_auth, "_transient_until", 0.0)
    monkeypatch.setattr(vertex_auth.time, "sleep", lambda s: None)
    seq = iter(answers)
    monkeypatch.setattr(vertex_auth, "_adc_mints", lambda: next(seq))
    monkeypatch.setattr(vertex_auth, "_refusal_confirmed", _REAL_CONFIRM)
    return fake


def test_a_refusal_that_clears_on_the_retry_raises_nothing(tmp_path, monkeypatch):
    fake = _arm(monkeypatch, tmp_path, [False, True])
    vertex_auth.touch_sentinel()
    assert not fake.exists()


def test_a_refusal_every_retry_confirms_raises_the_sentinel(tmp_path, monkeypatch):
    fake = _arm(monkeypatch, tmp_path, [False, False])
    vertex_auth.touch_sentinel()
    assert fake.exists()


def test_when_gcloud_cannot_be_asked_the_sentinel_still_goes_up(tmp_path, monkeypatch):
    fake = _arm(monkeypatch, tmp_path, [None, None])
    vertex_auth.touch_sentinel()
    assert fake.exists()


def test_a_batch_failing_row_after_row_waits_only_once(tmp_path, monkeypatch):
    calls = []

    def mints():
        calls.append(1)
        return True

    fake = _arm(monkeypatch, tmp_path, [])
    monkeypatch.setattr(vertex_auth, "_adc_mints", mints)
    for _ in range(5):
        vertex_auth.touch_sentinel()
    assert not fake.exists()
    assert len(calls) == 1

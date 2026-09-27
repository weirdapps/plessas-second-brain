"""Current credential shapes are redacted before they reach the store.

redact.py knew the classic shapes only. A fine-grained GitHub PAT, a Claude
Code OAuth token (sk-ant-oat01-), an Anthropic admin key, a Google OAuth access
token, a JWT bearer and a Healthchecks ping URL all passed through unchanged,
and an OpenAI project key with a dash early in its body was skipped or only
partly replaced. scrub_secrets reuses the same patterns, so it could not find
them afterwards either. Findings dedup-actions-1, security-3, scripts-maint-6.

Fixtures are low-entropy on purpose: they are shapes, not secrets, and a
high-entropy literal would trip gitleaks on every commit.
"""

import pytest

from src.redact import redact_secrets

_BODY = "a1B2c3D4e5" * 6  # 60 chars of [A-Za-z0-9]
_B64URL = "aB-c_D1e2F" * 6  # 60 chars with dashes and underscores throughout

SHAPES = {
    "github_pat": ("github-pat", "github_pat_" + "11AAAAAAA0" * 2 + "AA_" + "a1B2c3D4e5" * 6),
    "anthropic oat": ("anthropic-key", "sk-ant-oat01-" + _B64URL),
    "anthropic admin": ("anthropic-key", "sk-ant-admin01-" + _B64URL),
    "anthropic api": ("anthropic-key", "sk-ant-api03-" + _B64URL),
    "openai proj dashed": ("openai-key", "sk-proj-" + _B64URL),
    "openai proj dash first": ("openai-key", "sk-proj-" + "-" + _BODY),
    "openai svcacct": ("openai-key", "sk-svcacct-" + _B64URL),
    "openai admin": ("openai-key", "sk-admin-" + _B64URL),
    "openai plain": ("openai-key", "sk-" + _BODY),
    "google oauth": ("google-oauth", "ya29." + "a0AfB_byC" + _B64URL),
    "jwt": (
        "jwt",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        + ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkEifQ"
        + ".aaaaBBBBccccDDDDeeee-_ffff",
    ),
}


@pytest.mark.parametrize("key", sorted(SHAPES))
def test_each_shape_is_removed_whole_and_labelled(key):
    label, secret = SHAPES[key]
    out = redact_secrets(f"value: {secret} end")
    assert out == f"value: [REDACTED:{label}] end", out


def test_a_dashed_project_key_leaves_no_tail_in_clear():
    secret = "sk-proj-" + "abcdefghij" + "-" + "klmnopqrst" * 5
    out = redact_secrets(secret)
    assert "klmnopqrst" not in out, out


def test_google_oauth_token_keeps_a_sentence_full_stop():
    out = redact_secrets("token ya29." + _B64URL + ".")
    assert out == "token [REDACTED:google-oauth]."


@pytest.mark.parametrize(
    "url",
    [
        "https://hc-ping.com/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "https://hc-ping.com/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/fail",
        "https://hc-ping.com/aaaaBBBBccccDDDDeeee_-/nightly-backup",
    ],
)
def test_healthchecks_ping_urls_lose_their_check_id(url):
    out = redact_secrets(f"curl -fsS {url}")
    assert "0a1b2c3d-4e5f" not in out and "aaaaBBBBcccc" not in out, out
    assert "hc-ping.com/[REDACTED:healthchecks-ping]" in out, out


@pytest.mark.parametrize(
    "text",
    [
        "Καλησπέρα, στείλε μου το αρχείο με τα αποτελέσματα του τριμήνου.",
        "ΙΒΑΝ: GR16 0110 1250 0000 0001 2300 695",
        "IBAN GR1601101250000000012300695 για την πληρωμή",
        "sha256 " + "0123456789abcdef" * 4,
        "message id 0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d outside any ping URL",
        "https://hc-ping.com/ is the service, with no check id",
        "the prefixes github_pat_, sk-ant-oat01-, sk-proj-, ya29. and eyJhbGciOi alone",
        "a skeleton-key-that-is-not-a-secret-at-all-because-it-is-prose-only-words",
    ],
)
def test_ordinary_text_is_untouched(text):
    assert redact_secrets(text) == text

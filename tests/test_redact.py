"""Credential material must not reach the store.

An audit on 2026-09-09 found live secrets in brain.db: 10 conversation turns with
sk-ant-api03 keys (108-120 chars, so real keys, not the sk-ant-... placeholder in
.env.example), 13 with AIzaSy Google keys, 8 with 45-char ghp_ tokens. There was
no redaction anywhere on the ingest path. This store fans out (Vertex extraction
requests, rsync to every replica, months of encrypted snapshots), so a secret
that lands here lands in all of them.

These tests pin both halves: the patterns catch what they must, and they do NOT
fire on prose or on documentation placeholders.
"""

import json

from src.export.state import write_json_atomic
from src.redact import redact_payload, redact_secrets

REAL = {
    "anthropic-key": "sk-ant-api03-" + "a1B2c3D4e5" * 9,
    "google-key": "AIzaSy" + "A1b2C3d4E5" * 3 + "fghij",
    "github-token": "ghp_" + "a1B2c3D4e5" * 4,
    # Low-entropy on purpose. These are shape fixtures, not secrets, and a
    # high-entropy literal here would trip gitleaks on every commit.
    "slack-token": "xoxb-000000000000-000000000000-" + "a" * 24,
    "openai-key": "sk-" + "a1B2c3D4e5" * 5,
    "aws-key-id": "AKIAIOSFODNN7EXAMPLE",
    "telegram-token": "123456789:AA" + "a1B2c3D4e5" * 4,
}


class TestCatchesRealCredentials:
    def test_each_pattern_is_removed_and_labelled(self):
        for name, secret in REAL.items():
            out = redact_secrets(f"the value is {secret} ok")
            assert secret not in out, f"{name} survived redaction"
            assert f"[REDACTED:{name}]" in out, f"{name} was not labelled: {out}"

    def test_private_key_block_is_removed_whole(self):
        marker = "a" * 20
        # The PEM header alone trips gitleaks' private-key rule and the body here
        # is twenty letter a's, so there is no key material on the next line. The
        # annotation has to sit on that line to apply; it says so explicitly
        # rather than obscuring the shape, which would defeat the test.
        head = "-----BEGIN RSA PRIVATE KEY-----"  # gitleaks:allow
        tail = "-----END RSA PRIVATE KEY-----"  # gitleaks:allow
        body = f"{head}\n{marker}\n{tail}"
        out = redact_secrets(f"before\n{body}\nafter")
        assert marker not in out
        assert "[REDACTED:private-key]" in out
        assert "before" in out and "after" in out

    def test_several_secrets_in_one_string(self):
        text = f"{REAL['github-token']} and {REAL['google-key']}"
        out = redact_secrets(text)
        assert REAL["github-token"] not in out
        assert REAL["google-key"] not in out


class TestLeavesInnocentTextAlone:
    def test_documentation_placeholders_are_not_touched(self):
        """.env.example ships `ANTHROPIC_API_KEY=sk-ant-...`. Redacting that
        would make the gate look effective while catching nothing real.
        """
        for placeholder in ("sk-ant-...", "AIza...", "ghp_...", "sk-ant-api-key"):
            assert redact_secrets(placeholder) == placeholder

    def test_prose_is_unchanged(self):
        prose = "We decided to rotate the API key before the AKIA migration in Q3."
        assert redact_secrets(prose) == prose

    def test_empty_and_non_string_pass_through(self):
        assert redact_secrets("") == ""
        assert redact_secrets(None) is None


class TestPayloadWalk:
    def test_nested_structures_are_redacted(self):
        payload = {
            "emails": [{"content": f"key={REAL['google-key']}", "subject": "hi"}],
            "n": 3,
        }
        out = redact_payload(payload)
        assert REAL["google-key"] not in json.dumps(out)
        assert out["emails"][0]["subject"] == "hi"
        assert out["n"] == 3

    def test_dict_keys_are_left_alone(self):
        """Keys are field names; rewriting one breaks the staging contract."""
        out = redact_payload({"message_id": "x", "content": "y"})
        assert set(out) == {"message_id", "content"}


class TestStagingBoundary:
    def test_batch_writer_redacts_when_asked(self, tmp_path):
        p = tmp_path / "batch-00001.json"
        write_json_atomic(p, {"emails": [{"content": REAL["anthropic-key"]}]}, redact=True)
        assert REAL["anthropic-key"] not in p.read_text()
        assert "[REDACTED:anthropic-key]" in p.read_text()

    def test_state_writes_are_untouched_by_default(self, tmp_path):
        """redact defaults off so state files skip a walk with nothing to find."""
        p = tmp_path / "state.json"
        write_json_atomic(p, {"note": REAL["github-token"]})
        assert REAL["github-token"] in p.read_text()

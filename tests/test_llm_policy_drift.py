"""Drift guard for the vendored llm_policy.py.

Canonical copy: claude-config/shared/llm_policy.py
Propagate with: claude-config/scripts/sync-llm-policy.sh
Never edit the vendored copy directly; edit the canonical one and re-run the sync.
"""

import hashlib
from pathlib import Path

EXPECTED_SHA256 = "2b293c4d456e658d120281f4097fe8d55ac95d67e85d36dbad40a1d04b5fa95b"
# parents[N] where N = (path segments in the test's own repo-relative path) - 1,
# which lands on the repo root. tests/x.py -> parents[1]; a/b/tests/x.py -> parents[3].
MODULE = Path(__file__).resolve().parents[1] / "src/llm_policy.py"


def test_vendored_llm_policy_matches_the_canonical_copy():
    actual = hashlib.sha256(MODULE.read_bytes()).hexdigest()
    assert actual == EXPECTED_SHA256, (
        "llm_policy.py has drifted from the canonical copy. "
        "Edit claude-config/shared/llm_policy.py and run scripts/sync-llm-policy.sh."
    )

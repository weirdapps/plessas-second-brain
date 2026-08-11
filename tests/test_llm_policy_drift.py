"""Drift guard for the vendored llm_policy.py.

Canonical copy: claude-config/shared/llm_policy.py
Propagate with: claude-config/scripts/sync-llm-policy.sh
Never edit the vendored copy directly; edit the canonical one and re-run the sync.
"""

import hashlib
from pathlib import Path

EXPECTED_SHA256 = "0a1c9c42bbb9537e126f7eeedfc1179aa3c516ab7f6b6ec786ad693c9579b5c5"
# parents[N] where N = (path segments in the test's own repo-relative path) - 1,
# which lands on the repo root. tests/x.py -> parents[1]; a/b/tests/x.py -> parents[3].
MODULE = Path(__file__).resolve().parents[1] / "src/llm_policy.py"


def test_vendored_llm_policy_matches_the_canonical_copy():
    actual = hashlib.sha256(MODULE.read_bytes()).hexdigest()
    assert actual == EXPECTED_SHA256, (
        "llm_policy.py has drifted from the canonical copy. "
        "Edit claude-config/shared/llm_policy.py and run scripts/sync-llm-policy.sh."
    )

"""Drift guard for the vendored llm_policy.py.

Canonical copy: claude-config/shared/llm_policy.py
Propagate with: claude-config/scripts/sync-llm-policy.sh
Never edit the vendored copy directly; edit the canonical one and re-run the sync.
"""

import hashlib
from pathlib import Path

EXPECTED_SHA256 = "4046850b99b5faa56837350b3d9d33cc18ac11b7b0bf76c8215bd4c36f7295c8"
# parents[N] where N = (path segments in the test's own repo-relative path) - 1,
# which lands on the repo root. tests/x.py -> parents[1]; a/b/tests/x.py -> parents[3].
MODULE = Path(__file__).resolve().parents[1] / "src/llm_policy.py"


def test_vendored_llm_policy_matches_the_canonical_copy():
    actual = hashlib.sha256(MODULE.read_bytes()).hexdigest()
    assert actual == EXPECTED_SHA256, (
        "llm_policy.py has drifted from the canonical copy. "
        "Edit claude-config/shared/llm_policy.py and run scripts/sync-llm-policy.sh."
    )

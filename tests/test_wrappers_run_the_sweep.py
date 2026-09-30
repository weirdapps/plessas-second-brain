"""Both ingest wrappers end with the sweep, and the nightly pass reaps orphans first.

They pass --policy, so the policy file decides whether anything is deleted and no
hand-installed wrapper changes between rollout stages.
"""

from pathlib import Path

WRAPPERS = Path(__file__).resolve().parent.parent / "scripts" / "wrappers" / "systemd"


def test_the_hourly_sync_sweeps_after_it_loads():
    text = (WRAPPERS / "sb-outlook-sync.sh").read_text()
    assert "src.cli sweep-files --policy" in text
    assert text.index("sync --engine claude") < text.index("sweep-files --policy")


def test_the_nightly_pass_reaps_then_sweeps():
    text = (WRAPPERS / "sb-attachment-pass.sh").read_text()
    reap = text.index("reap_orphan_attachments.py --policy")
    sweep = text.index("src.cli sweep-files --policy")
    assert reap < sweep
    assert "--apply" not in text[reap : text.index("\n", reap)]

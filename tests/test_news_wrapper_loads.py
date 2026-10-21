"""sb-news-sync.sh loads what it stages, so News waits neither for Outlook nor its timer.

The only load of staged batches used to be sb-outlook-sync's, after its exports. So a
dead Outlook session (the wrapper exits at its sentinel before the load) held News back
for as long as the session stayed dead, and even on a healthy night the batch staged at
22:51 waited for the 01:10 run: 76 items were waiting at the 2026-10-20 audit. Extracting
news needs no model and the load no Outlook, so the news run now loads its own batch,
through `sync`'s lock like the other loaders.

The real wrapper runs against a throwaway HOME whose venv python records its arguments.
"""

import subprocess
from pathlib import Path

_WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-news-sync.sh"


def _home(tmp_path: Path, *sentinels: str, body: str = "exit 0\n") -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    news_db = home / "SourceCode" / "news" / "data" / "news.db"
    news_db.parent.mkdir(parents=True)
    news_db.write_text("")
    (home / ".second-brain").mkdir(parents=True)
    for sentinel in sentinels:
        (home / ".second-brain" / sentinel).touch()
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho "$*" >> "$HOME/calls.log"\n' + body)
    python.chmod(0o755)
    return home


def _run(home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPER)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(home: Path) -> list[str]:
    log = home / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_the_news_run_loads_what_it_staged(tmp_path):
    home = _home(tmp_path)

    result = _run(home)

    calls = _calls(home)
    assert calls[0] == "-m src.cli news-sync"
    (load,) = [c for c in calls if "src.cli sync" in c]
    args = load.split()
    assert "--skip-export" in args
    # Waits for a load already running rather than skipping at once: that one may have
    # started before this batch was staged.
    assert float(args[args.index("--lock-wait") + 1]) > 0
    assert result.returncode == 0


def test_a_dead_outlook_session_does_not_hold_news_back(tmp_path):
    home = _home(tmp_path, "needs_reauth")

    result = _run(home)

    assert any("src.cli sync" in c for c in _calls(home))
    assert result.returncode == 0


def test_a_dead_gcloud_credential_stages_and_skips_the_load_red(tmp_path):
    """News extracts without the model, but `sync` extracts the staged mail with it: the
    load waits for the credential, and the unit says so."""
    home = _home(tmp_path, "needs_gcloud_reauth")

    result = _run(home)

    calls = _calls(home)
    assert calls == ["-m src.cli news-sync"]
    assert result.returncode == 69
    log = (home / ".second-brain" / "logs" / "news-sync.log").read_text()
    assert "needs_gcloud_reauth" in log


def test_a_failed_staging_is_not_followed_by_a_load(tmp_path):
    home = _home(tmp_path, body='case "$*" in *news-sync*) exit 1;; esac\nexit 0\n')

    result = _run(home)

    assert not any("src.cli sync" in c for c in _calls(home))
    assert result.returncode == 1


def test_a_failed_load_fails_the_unit(tmp_path):
    home = _home(tmp_path, body='case "$*" in *"cli sync"*) exit 75;; esac\nexit 0\n')

    assert _run(home).returncode == 75


def test_a_missing_news_database_still_skips_quietly(tmp_path):
    """No upstream is a host without news-reader, not missing work (unchanged)."""
    home = _home(tmp_path)
    (home / "SourceCode" / "news" / "data" / "news.db").unlink()

    result = _run(home)

    assert _calls(home) == []
    assert result.returncode == 0

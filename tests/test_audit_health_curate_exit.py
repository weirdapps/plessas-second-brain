"""curate_documents_daily fails the unit when no classification succeeded (scripts-maint-2).

Every classify error was logged and skipped, and the run still ended
"Done. New placements: 0." with exit 0, so eleven days of failed runs in August
(expired auth, the ThinkingBlock parse bug) were green in systemd and in the
health check alike.
"""

import sys

import tests.test_curate_documents as tcd

# Fixtures borrowed from the existing curate suite, which owns the stub taxonomy
# and the three-table store.
curate = tcd.curate
brain = tcd.brain


def _seed(brain, *ids):
    for row_id in ids:
        tcd._seed_candidate(
            brain.conn,
            brain.src_dir,
            row_id=row_id,
            filename=f"deck{row_id}.pdf",
            mailbox_name="Inbox",
            message_id=f"AAMkADk1ZTRiexample{row_id}",
        )
    brain.conn.commit()


def _drive(curate, monkeypatch, classify):
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    monkeypatch.setattr(curate, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(curate, "_get_client_and_model", lambda: (object(), "model"))
    monkeypatch.setattr(curate, "classify_one", classify)
    monkeypatch.setattr(curate, "summarize_folder", lambda *a, **k: {"purpose": "stub"})
    monkeypatch.setattr(sys, "argv", ["curate_documents_daily.py"])
    return curate.main()


def test_a_run_where_every_classification_raised_exits_1(curate, brain, monkeypatch, capsys):
    _seed(brain, 1, 2)

    def broken(c):
        raise RuntimeError("Reauthentication is needed")

    assert _drive(curate, monkeypatch, broken) == 1
    done = [ln for ln in capsys.readouterr().out.splitlines() if "Done." in ln]
    assert done and "Classify errors: 2" in done[-1]
    # Nothing is lost: a failed id is not recorded as processed.
    assert tcd._state(curate)["processed_ids"] == []


def test_a_run_with_some_successes_still_exits_0(curate, brain, monkeypatch, capsys):
    _seed(brain, 1, 2)

    def half(c):
        if c["id"] == 2:
            raise RuntimeError("HTTP 401")
        return {"folder": "SKIP", "confidence": "low"}

    assert _drive(curate, monkeypatch, half) == 0
    done = [ln for ln in capsys.readouterr().out.splitlines() if "Done." in ln]
    assert done and "Classify errors: 1" in done[-1]

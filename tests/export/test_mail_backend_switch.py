"""SB_MAIL_BACKEND chooses who answers run_outlook_cli, and outlook-cli is always the way back.

outlook-cli (default)  outlook-cli only, exactly as before.
graph                  the Graph CLI; a verb it cannot serve still goes to outlook-cli.
auto                   the Graph CLI first; any failure of it falls back to outlook-cli
                       for that call, and the fallback is recorded.
shadow                 outlook-cli answers; the Graph CLI runs beside it on reads and
                       every disagreement is recorded. For proving the switch safe.
"""

import json
from unittest.mock import patch

import pytest

from src import config
from src.export import outlook_cli
from src.export.graph_cli import GraphCliAuthRequired, GraphCliError, GraphCliUnsupported
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError, run_outlook_cli
from tests.patching import unset_env


def _events():
    p = config.DATA_ROOT / "mail_backend_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


@pytest.fixture(autouse=True)
def _clean_events():
    p = config.DATA_ROOT / "mail_backend_events.jsonl"
    p.unlink(missing_ok=True)
    yield
    p.unlink(missing_ok=True)


def test_default_is_outlook_cli_and_never_touches_graph(monkeypatch):
    unset_env(monkeypatch, "SB_MAIL_BACKEND")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=[{"Id": "o"}]) as oc,
        patch("src.export.graph_cli.translate") as g,
    ):
        assert run_outlook_cli(["list-mail"]) == [{"Id": "o"}]
    oc.assert_called_once()
    g.assert_not_called()


def test_an_unknown_backend_name_is_outlook_cli(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "grpah")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=1),
        patch("src.export.graph_cli.translate") as g,
    ):
        assert run_outlook_cli(["auth-check"]) == 1
    g.assert_not_called()


def test_graph_answers_and_outlook_cli_is_not_called(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "graph")
    with (
        patch.object(outlook_cli, "_run_binary") as oc,
        patch("src.export.graph_cli.translate", return_value={"Id": "g"}),
    ):
        assert run_outlook_cli(["get-mail", "m", "--body", "html"]) == {"Id": "g"}
    oc.assert_not_called()


def test_graph_mode_sends_unsupported_verbs_to_outlook_cli(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "graph")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=["ev"]) as oc,
        patch("src.export.graph_cli.translate", side_effect=GraphCliUnsupported("calendar")),
    ):
        assert run_outlook_cli(["list-calendar", "--from", "a", "--to", "b"]) == ["ev"]
    oc.assert_called_once()


def test_graph_mode_turns_a_lost_login_into_the_auth_error_callers_know(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "graph")
    with patch("src.export.graph_cli.translate", side_effect=GraphCliAuthRequired("expired")):
        with pytest.raises(OutlookCliAuthRequired):
            run_outlook_cli(["get-mail", "m", "--body", "html"])


def test_graph_mode_turns_other_failures_into_outlook_cli_errors(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "graph")
    with patch("src.export.graph_cli.translate", side_effect=GraphCliError(1, "boom")):
        with pytest.raises(OutlookCliError) as exc:
            run_outlook_cli(["get-mail", "m", "--body", "html"])
    assert not isinstance(exc.value, OutlookCliAuthRequired)


@pytest.mark.parametrize(
    "failure",
    [GraphCliAuthRequired("expired"), GraphCliError(1, "boom")],
)
def test_auto_falls_back_to_outlook_cli_on_any_graph_failure_and_records_it(monkeypatch, failure):
    monkeypatch.setenv("SB_MAIL_BACKEND", "auto")
    with (
        patch.object(outlook_cli, "_run_binary", return_value={"Id": "o"}) as oc,
        patch("src.export.graph_cli.translate", side_effect=failure),
    ):
        assert run_outlook_cli(["get-mail", "m", "--body", "html"]) == {"Id": "o"}
    oc.assert_called_once()
    ev = _events()
    assert ev and ev[-1]["outcome"] == "fallback" and ev[-1]["verb"] == "get-mail"
    assert ev[-1]["exit_code"] == failure.exit_code
    assert ev[-1]["reason"] == ("auth" if failure.exit_code == 2 else "error")


def test_auto_routes_an_unsupported_call_to_outlook_cli_without_calling_it_a_fallback(monkeypatch):
    # Calendar verbs are outlook-cli's by design; a record per call would bury real fallbacks.
    monkeypatch.setenv("SB_MAIL_BACKEND", "auto")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=["ev"]) as oc,
        patch("src.export.graph_cli.translate", side_effect=GraphCliUnsupported("list-calendar")),
    ):
        assert run_outlook_cli(["list-calendar"]) == ["ev"]
    oc.assert_called_once()
    assert _events() == []


def test_auto_falls_back_when_graph_times_out(monkeypatch):
    import subprocess

    monkeypatch.setenv("SB_MAIL_BACKEND", "auto")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=[]) as oc,
        patch("src.export.graph_cli.translate", side_effect=subprocess.TimeoutExpired("g", 60)),
    ):
        assert run_outlook_cli(["list-mail"]) == []
    oc.assert_called_once()
    assert _events()[-1]["reason"] == "timeout"


@pytest.mark.parametrize("failure", [ValueError("bad json"), KeyError("id"), OSError("disk")])
def test_auto_falls_back_on_an_unexpected_graph_exception_too(monkeypatch, failure):
    # A parse or shape bug in the adapter must cost one fallback, never the sync run.
    monkeypatch.setenv("SB_MAIL_BACKEND", "auto")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=[]) as oc,
        patch("src.export.graph_cli.translate", side_effect=failure),
    ):
        assert run_outlook_cli(["list-mail"]) == []
    oc.assert_called_once()
    ev = _events()[-1]
    assert ev["outcome"] == "fallback" and ev["reason"] == "unexpected"


def test_auto_success_does_not_call_outlook_cli_and_is_recorded(monkeypatch):
    # The fallback rate needs a denominator: every call Graph served is one line.
    monkeypatch.setenv("SB_MAIL_BACKEND", "auto")
    with (
        patch.object(outlook_cli, "_run_binary") as oc,
        patch("src.export.graph_cli.translate", return_value=[{"Id": "g"}]),
    ):
        assert run_outlook_cli(["list-mail"]) == [{"Id": "g"}]
    oc.assert_not_called()
    assert _events() == [
        {"at": _events()[0]["at"], "backend": "auto", "verb": "list-mail", "outcome": "ok"}
    ]


def test_auto_when_both_fail_raises_outlook_clis_error(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "auto")
    with (
        patch.object(outlook_cli, "_run_binary", side_effect=OutlookCliAuthRequired("x")),
        patch("src.export.graph_cli.translate", side_effect=GraphCliError(1, "boom")),
    ):
        with pytest.raises(OutlookCliAuthRequired):
            run_outlook_cli(["list-mail"])


def test_shadow_returns_outlook_cli_and_records_a_listing_disagreement(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "shadow")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=[{"Id": "a"}, {"Id": "b"}]),
        patch("src.export.graph_cli.translate", return_value=[{"Id": "a"}, {"Id": "c"}]),
    ):
        assert run_outlook_cli(["list-mail", "--folder", "Inbox"]) == [{"Id": "a"}, {"Id": "b"}]
    ev = _events()[-1]
    assert ev["outcome"] == "mismatch"
    assert ev["only_outlook_cli"] == ["b"] and ev["only_graph"] == ["c"]


def test_shadow_records_agreement_on_a_message(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "shadow")
    msg = {
        "Id": "a",
        "InternetMessageId": "<a>",
        "ConversationId": "c",
        "Subject": "s",
        "Body": {"Content": "<b>x</b>"},
    }
    with (
        patch.object(outlook_cli, "_run_binary", return_value=msg),
        patch("src.export.graph_cli.translate", return_value=dict(msg)),
    ):
        run_outlook_cli(["get-mail", "a", "--body", "html"])
    assert _events()[-1]["outcome"] == "match"


def test_shadow_never_runs_graph_downloads(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "shadow")
    with (
        patch.object(outlook_cli, "_run_binary", return_value={"saved": []}),
        patch("src.export.graph_cli.translate") as g,
    ):
        run_outlook_cli(["download-attachments", "m", "--out", "/tmp/x"])
    g.assert_not_called()


def test_shadow_swallows_graph_failures(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "shadow")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=[]),
        patch("src.export.graph_cli.translate", side_effect=GraphCliError(1, "boom")),
    ):
        assert run_outlook_cli(["list-mail"]) == []
    assert _events()[-1]["outcome"] == "graph_error"


def _vanishing_translate(rows, gone):
    """A Graph listing that lost `gone` because Exchange no longer had them at read time."""
    from src.export import graph_cli

    def fake(args, timeout_sec=60):
        graph_cli._VANISHED.ids = list(gone)
        return rows

    return fake


def test_shadow_does_not_count_a_vanished_message_as_a_mismatch(monkeypatch):
    # VPS 2026-11-09 10:13Z: outlook-cli listed m2, Graph listed it too, and Exchange
    # answered "not found in the store" when Graph read it a moment later.
    monkeypatch.setenv("SB_MAIL_BACKEND", "shadow")
    with (
        patch.object(outlook_cli, "_run_binary", return_value=[{"Id": "m1"}, {"Id": "m2"}]),
        patch("src.export.graph_cli.translate", _vanishing_translate([{"Id": "m1"}], ["m2"])),
    ):
        run_outlook_cli(["list-mail", "--select", "Id,InternetMessageId"])
    ev = _events()
    shadow = [e for e in ev if e["backend"] == "shadow"]
    assert [e["outcome"] for e in shadow] == ["match"]
    assert shadow[0]["vanished"] == 1
    gone = [e for e in ev if e["outcome"] == "vanished"]
    assert gone and gone[0]["ids"] == ["m2"] and gone[0]["mode"] == "shadow"
    # Never a shadow line: the estate grader reads only those, and a vanished
    # message is no disagreement between the backends.
    assert gone[0]["backend"] != "shadow"


def test_shadow_still_flags_a_real_disagreement_beside_a_vanished_one(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "shadow")
    with (
        patch.object(
            outlook_cli, "_run_binary", return_value=[{"Id": "m1"}, {"Id": "m2"}, {"Id": "m3"}]
        ),
        patch("src.export.graph_cli.translate", _vanishing_translate([{"Id": "m1"}], ["m2"])),
    ):
        run_outlook_cli(["list-mail", "--select", "Id,InternetMessageId"])
    shadow = [e for e in _events() if e["backend"] == "shadow"]
    assert shadow[0]["outcome"] == "mismatch" and shadow[0]["only_outlook_cli"] == ["m3"]


def test_auto_logs_vanished_messages_without_counting_them_as_calls(monkeypatch):
    monkeypatch.setenv("SB_MAIL_BACKEND", "auto")
    with (
        patch.object(outlook_cli, "_run_binary") as oc,
        patch("src.export.graph_cli.translate", _vanishing_translate([{"Id": "m1"}], ["m2"])),
    ):
        assert run_outlook_cli(["list-mail"]) == [{"Id": "m1"}]
    oc.assert_not_called()
    outcomes = sorted(e["outcome"] for e in _events())
    assert outcomes == ["ok", "vanished"]

"""The Graph CLI adapter answers outlook-cli's verbs in outlook-cli's shape.

Every caller reads Outlook REST v2.0 PascalCase (`Id`, `Body.Content`,
`From.EmailAddress.Address`). The Graph CLI speaks camelCase and its own verbs,
so the adapter translates both ways. Message ids are the same on both sides (an
Exchange item id), which is what makes a per-call fallback safe.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.export import graph_cli
from src.export.graph_cli import (
    GraphCliAuthRequired,
    GraphCliError,
    GraphCliUnsupported,
    translate,
)


def _proc(payload, returncode=0, stderr=""):
    m = MagicMock()
    m.stdout = json.dumps(payload) if not isinstance(payload, str) else payload
    m.stderr = stderr
    m.returncode = returncode
    return m


def _full(mid, subject="s"):
    return {
        "id": mid,
        "subject": subject,
        "receivedDateTime": "2026-11-06T13:43:25Z",
        "internetMessageId": f"<{mid}@x>",
        "conversationId": f"c-{mid}",
        "hasAttachments": False,
        "isRead": True,
        "webLink": "https://x/" + mid,
        "from": {"emailAddress": {"name": "A", "address": "a@example.com"}},
        "toRecipients": [{"emailAddress": {"name": "B", "address": "b@example.com"}}],
        "ccRecipients": [],
        "body": {"contentType": "html", "content": "<html>hi</html>"},
    }


@pytest.fixture(autouse=True)
def _binary(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_CLI_PATH", "/opt/graph-cli")
    graph_cli._READ_CACHE.clear()


def _calls(mock_run):
    return [c.args[0] for c in mock_run.call_args_list]


@patch("src.export.graph_cli._spawn")
def test_get_mail_returns_pascal_case_with_the_html_body(mock_run):
    mock_run.return_value = _proc({"data": _full("AAMk1")})
    msg = translate(["get-mail", "AAMk1", "--body", "html"])
    assert msg["Id"] == "AAMk1"
    assert msg["From"]["EmailAddress"]["Address"] == "a@example.com"
    assert msg["ToRecipients"][0]["EmailAddress"]["Name"] == "B"
    assert msg["Body"] == {"ContentType": "HTML", "Content": "<html>hi</html>"}
    assert msg["InternetMessageId"] == "<AAMk1@x>"
    cmd = _calls(mock_run)[0]
    assert cmd[:4] == ["/opt/graph-cli", "mail", "read", "AAMk1"]
    assert "--full" in cmd and "--json" in cmd


@patch("src.export.graph_cli._spawn")
def test_every_call_runs_with_tz_utc(mock_run):
    mock_run.return_value = _proc({"data": _full("AAMk1")})
    translate(["get-mail", "AAMk1", "--body", "html"])
    assert mock_run.call_args.kwargs["env"]["TZ"] == "UTC"


@patch("src.export.graph_cli._spawn")
def test_get_mail_body_none_is_refused_because_it_means_the_attachment_list(mock_run):
    # outlook-cli's get-mail embeds the attachment list; the Graph read does not,
    # and an empty list would read as "no attachments".
    with pytest.raises(GraphCliUnsupported):
        translate(["get-mail", "AAMk1", "--body", "none"])
    mock_run.assert_not_called()


@patch("src.export.graph_cli._spawn")
def test_list_with_native_fields_pages_until_next_skip_is_null(mock_run):
    page1 = {
        "data": [
            {
                "id": "m1",
                "date": "2026-11-06T10:00:00Z",
                "subject": "a",
                "attachments": "no",
                "read": "yes",
            },
            {
                "id": "m2",
                "date": "2026-11-06T09:00:00Z",
                "subject": "b",
                "attachments": "yes",
                "read": "no",
            },
        ],
        "nextSkip": 2,
    }
    page2 = {
        "data": [
            {
                "id": "m3",
                "date": "2026-11-06T08:00:00Z",
                "subject": "c",
                "attachments": "no",
                "read": "no",
            }
        ],
        "nextSkip": None,
    }
    mock_run.side_effect = [_proc(page1), _proc(page2)]
    out = translate(
        [
            "list-mail",
            "--folder",
            "Archive",
            "--select",
            "Id,ReceivedDateTime",
            "--since",
            "2026-11-05T22:00:00+00:00",
            "--until",
            "2026-11-07T00:00:00.123Z",
            "--all",
            "--max",
            "100",
        ]
    )
    assert out == [
        {"Id": "m1", "ReceivedDateTime": "2026-11-06T10:00:00Z"},
        {"Id": "m2", "ReceivedDateTime": "2026-11-06T09:00:00Z"},
        {"Id": "m3", "ReceivedDateTime": "2026-11-06T08:00:00Z"},
    ]
    first, second = _calls(mock_run)
    assert first[first.index("--folder") + 1] == "Archive"
    assert first[first.index("--after") + 1] == "2026-11-05T22:00:00Z"
    assert first[first.index("--before") + 1] == "2026-11-07T00:00:00Z"
    assert first[first.index("--top") + 1] == "1000"
    assert second[second.index("--skip") + 1] == "2"


@patch("src.export.graph_cli._spawn")
def test_list_stops_at_max(mock_run):
    rows = [{"id": f"m{i}", "date": "2026-11-06T10:00:00Z"} for i in range(5)]
    mock_run.return_value = _proc({"data": rows, "nextSkip": 5})
    out = translate(
        ["list-mail", "--select", "Id", "--since", "2026-11-01T00:00:00Z", "--all", "--max", "3"]
    )
    assert [m["Id"] for m in out] == ["m0", "m1", "m2"]
    assert mock_run.call_count == 1


@patch("src.export.graph_cli._spawn")
def test_list_without_all_returns_top_n_newest(mock_run):
    rows = [{"id": f"m{i}", "date": "2026-11-06T10:00:00Z"} for i in range(3)]
    mock_run.return_value = _proc({"data": rows, "nextSkip": 3})
    out = translate(["list-mail", "--folder", "Inbox", "--select", "Id", "--top", "3"])
    assert len(out) == 3
    cmd = _calls(mock_run)[0]
    assert cmd[cmd.index("--top") + 1] == "3"
    assert "--after" not in cmd


@patch("src.export.graph_cli._spawn")
def test_a_page_overlap_never_duplicates_a_message(mock_run):
    # New mail arriving between pages shifts $skip: the same id comes back twice.
    p1 = {"data": [{"id": "m1", "date": "d"}, {"id": "m2", "date": "d"}], "nextSkip": 2}
    p2 = {"data": [{"id": "m2", "date": "d"}, {"id": "m3", "date": "d"}], "nextSkip": None}
    mock_run.side_effect = [_proc(p1), _proc(p2)]
    out = translate(
        ["list-mail", "--select", "Id", "--since", "2026-11-01T00:00:00Z", "--all", "--max", "50"]
    )
    assert [m["Id"] for m in out] == ["m1", "m2", "m3"]


@patch("src.export.graph_cli._spawn")
def test_fields_the_listing_lacks_are_read_per_message_and_cached(mock_run):
    listing = {"data": [{"id": "m1", "date": "2026-11-06T10:00:00Z"}], "nextSkip": None}
    mock_run.side_effect = [_proc(listing), _proc({"data": _full("m1")})]
    out = translate(
        [
            "list-mail",
            "--select",
            "Id,InternetMessageId,From",
            "--since",
            "2026-11-01T00:00:00Z",
            "--all",
            "--max",
            "9",
        ]
    )
    assert out == [
        {
            "Id": "m1",
            "InternetMessageId": "<m1@x>",
            "From": {"EmailAddress": {"Name": "A", "Address": "a@example.com"}},
        }
    ]
    # The hourly sync's get-mail right after is served from the cache.
    body = translate(["get-mail", "m1", "--body", "html"])
    assert body["Body"]["Content"] == "<html>hi</html>"
    assert mock_run.call_count == 2


@patch("src.export.graph_cli._spawn")
def test_a_listing_too_big_to_enrich_is_refused(mock_run, monkeypatch):
    monkeypatch.setenv("SB_GRAPH_ENRICH_MAX", "2")
    rows = [{"id": f"m{i}", "date": "d"} for i in range(3)]
    mock_run.return_value = _proc({"data": rows, "nextSkip": None})
    with pytest.raises(GraphCliUnsupported):
        translate(
            [
                "list-mail",
                "--select",
                "Id,InternetMessageId",
                "--since",
                "2026-11-01T00:00:00Z",
                "--all",
                "--max",
                "9",
            ]
        )
    assert mock_run.call_count == 1


@patch("src.export.graph_cli._spawn")
def test_download_skips_inline_unless_asked_and_reports_bytes_on_disk(mock_run, tmp_path):
    def fake(cmd, **kw):
        if cmd[1:4] == ["mail", "attachments", "list"]:
            return _proc(
                {
                    "data": [
                        {"name": "logo.png", "inline": "yes", "contentId": "cid1"},
                        {"name": "report.xlsx", "inline": "no", "contentId": ""},
                    ]
                }
            )
        out = cmd[cmd.index("--out") + 1]
        for name in ("logo.png", "report.xlsx"):
            (tmp_path / name).write_bytes(b"12345")
        return _proc(
            {
                "data": {
                    "saved": [
                        {"name": "logo.png", "path": f"{out}/logo.png", "size": 999},
                        {"name": "report.xlsx", "path": f"{out}/report.xlsx", "size": 999},
                    ]
                }
            }
        )

    mock_run.side_effect = fake
    res = translate(["download-attachments", "m1", "--out", str(tmp_path), "--overwrite"])
    assert [s["name"] for s in res["saved"]] == ["report.xlsx"]
    assert res["saved"][0]["size"] == 5
    assert not (tmp_path / "logo.png").exists()
    assert res["skipped"] == [{"name": "logo.png", "reason": "inline"}]
    dl = [c for c in _calls(mock_run) if "download" in c][0]
    assert "--force" in dl


@patch("src.export.graph_cli._spawn")
def test_download_with_include_inline_keeps_everything(mock_run, tmp_path):
    (tmp_path / "logo.png").write_bytes(b"1")
    mock_run.return_value = _proc(
        {"data": {"saved": [{"name": "logo.png", "path": str(tmp_path / "logo.png")}]}}
    )
    res = translate(["download-attachments", "m1", "--out", str(tmp_path), "--include-inline"])
    assert [s["name"] for s in res["saved"]] == ["logo.png"]
    assert mock_run.call_count == 1


@patch("src.export.graph_cli._spawn")
def test_exit_2_is_auth_required(mock_run):
    mock_run.return_value = _proc("", returncode=2, stderr="Not signed in")
    with pytest.raises(GraphCliAuthRequired):
        translate(["get-mail", "m1", "--body", "html"])


@patch("src.export.graph_cli._spawn")
def test_exit_1_is_a_graph_cli_error(mock_run):
    mock_run.return_value = _proc("", returncode=1, stderr="Mail folder not found")
    with pytest.raises(GraphCliError) as exc:
        translate(["list-mail", "--folder", "Nope", "--top", "1"])
    assert exc.value.exit_code == 1
    assert not isinstance(exc.value, GraphCliAuthRequired)


@patch("src.export.graph_cli._spawn")
def test_auth_check_reads_status(mock_run):
    mock_run.return_value = _proc({"data": {"status": "signed in", "email": "x@example.com"}})
    assert translate(["auth-check"])["ok"] is True
    assert _calls(mock_run)[0][1] == "status"


@pytest.mark.parametrize("verb", ["list-calendar", "get-event", "get-thread", "search"])
def test_verbs_the_graph_cli_cannot_serve_are_refused(verb):
    with pytest.raises(GraphCliUnsupported):
        translate([verb, "x"])


def test_no_binary_configured_is_unsupported(monkeypatch):
    monkeypatch.delenv("GRAPH_CLI_PATH")
    with pytest.raises(GraphCliUnsupported):
        translate(["get-mail", "m1", "--body", "html"])


def test_spawn_reads_all_of_a_large_output_through_a_file(tmp_path):
    # The real failure was a pipe cut at 64 KiB; this writes 300 KB through a real process.
    import sys

    from src.export.graph_cli import _spawn

    script = tmp_path / "big.py"
    script.write_text(
        "import sys, json; sys.stdout.write(json.dumps({'data': ['x' * 100] * 3000}))"
    )
    res = _spawn([sys.executable, "-I", str(script)], timeout_sec=30, env={})
    assert res.returncode == 0
    assert len(json.loads(res.stdout)["data"]) == 3000


@patch("src.export.graph_cli._spawn")
def test_enum_values_come_back_pascal_case_as_outlook_cli_writes_them(mock_run):
    msg = {
        **_full("m1"),
        "importance": "normal",
        "inferenceClassification": "focused",
        "flag": {"flagStatus": "notFlagged"},
    }
    mock_run.return_value = _proc({"data": msg})
    out = translate(["get-mail", "m1", "--body", "html"])
    assert out["Importance"] == "Normal"
    assert out["InferenceClassification"] == "Focused"
    assert out["Flag"] == {"FlagStatus": "NotFlagged"}


_GONE = (
    "Graph API Error: Request to /me/messages/m2 failed: "
    "The specified object was not found in the store., The process failed to get th"
)


@patch("src.export.graph_cli._spawn")
def test_a_message_gone_between_listing_and_read_is_dropped_not_fatal(mock_run):
    # VPS 2026-11-09 10:13Z: one message moved or deleted after the Graph listing and
    # before its enrichment read, and that one 404 failed the whole listing.
    listing = {"data": [{"id": "m1", "date": "d"}, {"id": "m2", "date": "d"}], "nextSkip": None}
    mock_run.side_effect = [
        _proc(listing),
        _proc({"data": _full("m1")}),
        _proc("", returncode=1, stderr=_GONE),
    ]
    out = translate(["list-mail", "--select", "Id,InternetMessageId", "--all", "--max", "9"])
    assert out == [{"Id": "m1", "InternetMessageId": "<m1@x>"}]
    # The dispatcher hears which ones, once, so it can log them as vanished.
    assert graph_cli.take_vanished() == ["m2"]
    assert graph_cli.take_vanished() == []


@patch("src.export.graph_cli._spawn")
def test_any_other_read_failure_during_enrichment_still_fails_the_listing(mock_run):
    listing = {"data": [{"id": "m1", "date": "d"}], "nextSkip": None}
    mock_run.side_effect = [_proc(listing), _proc("", returncode=1, stderr="network down")]
    with pytest.raises(GraphCliError):
        translate(["list-mail", "--select", "Id,InternetMessageId", "--all", "--max", "9"])

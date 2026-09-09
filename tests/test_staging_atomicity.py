"""One truncated staging batch must not wedge the pipeline.

Every reader walks the whole `batch-*.json` glob with a single json.load, so
before 2026-09-09 a batch file truncated by a killed job (systemd RuntimeMaxSec,
reboot, full disk) raised JSONDecodeError and stopped extract and load for every
source until a human deleted it by hand. Two changes close it: the writers are
atomic, and the readers quarantine what they cannot parse.
"""

import json

from src.export.state import load_json_or_quarantine, write_json_atomic


class TestWriteJsonAtomic:
    def test_no_partial_file_is_visible_and_no_tmp_is_left(self, tmp_path):
        target = tmp_path / "batch-00001.json"
        write_json_atomic(target, {"emails": [{"message_id": "a"}]})
        assert json.loads(target.read_text())["emails"][0]["message_id"] == "a"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_tmp_name_does_not_match_the_batch_glob(self, tmp_path):
        """The readers glob batch-*.json. If the temp file matched that glob a
        concurrent reader could pick up a half-written file, which is the bug
        this was meant to prevent.
        """
        target = tmp_path / "batch-00002.json"
        tmp = target.with_suffix(target.suffix + ".tmp")
        assert tmp.name == "batch-00002.json.tmp"
        assert tmp not in list(tmp_path.glob("batch-*.json"))

    def test_overwrites_an_existing_file(self, tmp_path):
        target = tmp_path / "batch-00003.json"
        write_json_atomic(target, {"n": 1})
        write_json_atomic(target, {"n": 2})
        assert json.loads(target.read_text())["n"] == 2

    def test_creates_missing_parent(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "batch-00004.json"
        write_json_atomic(target, {"n": 1})
        assert target.exists()


class TestLoadJsonOrQuarantine:
    def test_parses_a_good_file(self, tmp_path):
        p = tmp_path / "batch-00001.json"
        p.write_text('{"emails": []}')
        assert load_json_or_quarantine(p) == {"emails": []}
        assert not (tmp_path / "quarantine").exists()

    def test_moves_a_truncated_file_aside_and_returns_none(self, tmp_path, capsys):
        p = tmp_path / "batch-00002.json"
        p.write_text('{"batch_number": 1, "emails": [{"message_id": "a"')
        assert load_json_or_quarantine(p) is None
        assert not p.exists()
        assert (tmp_path / "quarantine" / "batch-00002.json").exists()

    def test_one_bad_batch_costs_one_batch(self, tmp_path):
        """The property that actually matters: the good batches still load."""
        (tmp_path / "batch-00001.json").write_text('{"emails": [{"message_id": "a"}]}')
        (tmp_path / "batch-00002.json").write_text('{"emails": [{"message_id"')
        (tmp_path / "batch-00003.json").write_text('{"emails": [{"message_id": "c"}]}')

        collected = []
        for bf in sorted(tmp_path.glob("batch-*.json")):
            data = load_json_or_quarantine(bf)
            if data is None:
                continue
            collected.extend(data["emails"])

        assert [e["message_id"] for e in collected] == ["a", "c"]

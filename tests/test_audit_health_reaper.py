"""The orphan reaper deletes only files byte-identical to a surviving copy (attachments-1).

It matched on (filename, size) alone. On the VPS, 402 registered name-and-size
groups hold more than one distinct content ('Daily Report.xlsx' and the like),
so an orphan holding the only copy of one day's report was unlinked because a
different day's report happened to weigh the same.
"""

import tests.test_orphan_reaper as tor

# Fixtures borrowed from the existing reaper suite, which owns the file-backed
# store and the redirected adoption root.
db_path = tor.db_path
db = tor.db


def test_same_name_and_size_but_different_bytes_is_adopted_not_deleted(db, db_path, tmp_path):
    tor._registered(db, tmp_path, "AAMk-monday", "Daily Report.xlsx", b"a" * 4096)
    dead = tor._orphan(tmp_path, "AAMk-tuesday", {"Daily Report.xlsx": b"b" * 4096})

    result = tor.reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert result["deleted"] == 0
    assert result["adopted"] == 1
    rows = db.execute(
        "SELECT COUNT(*) FROM attachments WHERE filename = 'Daily Report.xlsx'"
    ).fetchone()[0]
    assert rows == 2, "tuesday's report must be in the table, not the bin"
    assert not dead.exists()


def test_the_dry_run_does_not_count_a_different_file_as_a_duplicate(db, db_path, tmp_path):
    tor._registered(db, tmp_path, "AAMk-monday", "Daily Report.xlsx", b"a" * 4096)
    tor._orphan(tmp_path, "AAMk-tuesday", {"Daily Report.xlsx": b"b" * 4096})

    result = tor.reap_orphan_attachments(db_path, tmp_path, apply=False)

    assert result["deleted"] == 0
    assert result["adopted"] == 1

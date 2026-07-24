"""Tests for review-gated fuzzy people matching (src/store/fuzzy_people.py)."""

from src.store.fuzzy_people import (
    apply_reviewed_merges,
    find_fuzzy_candidates,
    read_review_file,
    similarity,
    write_review_file,
)
from src.store.schema import create_database


def _p(conn, name, email=None):
    cur = conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email))
    return cur.lastrowid


class TestSimilarity:
    def test_subset_scores_high(self):
        assert similarity("Anderson", "Ioannis Anderson") >= 0.9

    def test_unrelated_scores_low(self):
        assert similarity("Maria Novak", "Kostas Meyer") < 0.5


class TestFindCandidates:
    def test_finds_emailless_nickname_variant(self):
        conn = create_database(":memory:")
        _p(conn, "Ioannis Anderson", "anderson.ioannis@example.com")
        _p(conn, "Yiannis Anderson")  # emailless nickname variant, same surname block
        conn.commit()
        cands = find_fuzzy_candidates(conn, threshold=0.8)
        assert any(
            {c["name_a"], c["name_b"]} == {"Ioannis Anderson", "Yiannis Anderson"} for c in cands
        )
        conn.close()

    def test_skips_two_distinct_emails(self):
        conn = create_database(":memory:")
        _p(conn, "Maria Papadopoulou", "a@example.com")
        _p(conn, "Maroula Papadopoulou", "b@example.com")  # distinct emails => distinct people
        conn.commit()
        assert find_fuzzy_candidates(conn, threshold=0.7) == []
        conn.close()

    def test_skips_same_canonical_phase7_territory(self):
        conn = create_database(":memory:")
        _p(conn, "Papadopoulos Nikos")
        _p(conn, "Nikos Papadopoulos", "dp@example.com")  # same canonical -> phase 7 handles it
        conn.commit()
        assert find_fuzzy_candidates(conn, threshold=0.5) == []
        conn.close()


class TestReviewFileRoundTrip:
    def test_write_then_read(self, tmp_path):
        cands = [
            {
                "id_a": 1,
                "name_a": "A",
                "email_a": None,
                "id_b": 2,
                "name_b": "B",
                "email_b": "b@x",
                "score": 0.9,
                "decision": "",
            }
        ]
        path = str(tmp_path / "review.jsonl")
        assert write_review_file(cands, path) == 1
        assert read_review_file(path) == cands


class TestApply:
    def test_applies_only_merge_decisions(self):
        conn = create_database(":memory:")
        a = _p(conn, "Ioannis Anderson", "x@example.com")
        b = _p(conn, "Yiannis Anderson")  # emailless, decision=merge
        c = _p(conn, "Kostas Nakos", "y@example.com")
        d = _p(conn, "Konstantinos Nakos")  # emailless, decision blank -> untouched
        conn.commit()
        rows = [
            {
                "id_a": a,
                "email_a": "x@example.com",
                "id_b": b,
                "email_b": None,
                "decision": "merge",
            },
            {"id_a": c, "email_a": "y@example.com", "id_b": d, "email_b": None, "decision": ""},
        ]
        assert apply_reviewed_merges(conn, rows) == 1
        # a (email-bearing) survives; b merged away.
        assert conn.execute("SELECT COUNT(*) FROM people WHERE id=?", (b,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM people WHERE id=?", (a,)).fetchone()[0] == 1
        # c/d untouched (blank decision).
        assert conn.execute("SELECT COUNT(*) FROM people WHERE id=?", (d,)).fetchone()[0] == 1
        conn.close()

    def test_reassigns_references_on_apply(self):
        conn = create_database(":memory:")
        keep = _p(conn, "Ioannis Anderson", "x@example.com")
        remove = _p(conn, "Yiannis Anderson")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (1, 1, '2026-01-01', 's')"
        )
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (1, ?, 'sender')",
            (remove,),
        )
        conn.commit()
        rows = [
            {
                "id_a": keep,
                "email_a": "x@example.com",
                "id_b": remove,
                "email_b": None,
                "decision": "merge",
            }
        ]
        apply_reviewed_merges(conn, rows)
        who = conn.execute("SELECT person_id FROM email_people WHERE email_id=1").fetchone()[0]
        assert who == keep  # reference followed the merge
        conn.close()

"""Tests for Reciprocal Rank Fusion (src/store/fusion.py)."""

from src.store.fusion import reciprocal_rank_fusion

K = 60  # default damping constant


def _score(rank0: int) -> float:
    """RRF contribution of a single list at 0-based position rank0."""
    return 1.0 / (K + rank0 + 1)


class TestReciprocalRankFusion:
    def test_empty_inputs(self):
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([[], []]) == []

    def test_single_list_preserves_order(self):
        fused = reciprocal_rank_fusion([["a", "b", "c"]])
        assert [item for item, _ in fused] == ["a", "b", "c"]

    def test_item_in_both_lists_outranks_single_list_items(self):
        fused = dict(reciprocal_rank_fusion([["x", "a", "b"], ["x", "c", "d"]]))
        assert fused["x"] > fused["a"]
        assert fused["x"] > fused["c"]

    def test_consensus_beats_a_single_top_rank(self):
        # Ranked 2nd in BOTH lists should beat something ranked 1st in only one.
        fused = dict(reciprocal_rank_fusion([["top1", "consensus"], ["top2", "consensus"]]))
        assert fused["consensus"] > fused["top1"]
        assert fused["consensus"] > fused["top2"]

    def test_scores_match_formula(self):
        fused = dict(reciprocal_rank_fusion([["a", "b"], ["b", "a"]]))
        # a: rank0 in L1 + rank1 in L2; b: rank1 in L1 + rank0 in L2 -> equal
        assert abs(fused["a"] - (_score(0) + _score(1))) < 1e-12
        assert abs(fused["b"] - (_score(1) + _score(0))) < 1e-12

    def test_duplicate_within_list_counted_once(self):
        fused = dict(reciprocal_rank_fusion([["a", "a", "b"]]))
        assert abs(fused["a"] - _score(0)) < 1e-12  # first (best) occurrence only
        assert abs(fused["b"] - _score(2)) < 1e-12  # dup did not shift b's rank

    def test_custom_k(self):
        fused = dict(reciprocal_rank_fusion([["a"]], k=10))
        assert abs(fused["a"] - 1.0 / 11) < 1e-12

    def test_output_sorted_descending(self):
        fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "b"]])
        scores = [s for _, s in fused]
        assert scores == sorted(scores, reverse=True)

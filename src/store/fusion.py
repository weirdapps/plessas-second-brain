"""Reciprocal Rank Fusion (RRF) for combining ranked retrieval lists.

RRF merges several independently-ranked result lists into one ranking using only
each item's *rank position* — not its raw score — so heterogeneous signals (BM25
keyword rank and cosine-similarity rank) combine without any score normalization:

    score(d) = sum over lists L of  w_L / (k + rank_L(d))

where rank is 1-based, k dampens the weight of top ranks and w_L is the list's
weight (1 unless given). k=60 is the standard default from Cormack et al. (SIGIR
2009) and matches the value used by OpenSearch / Elasticsearch / Weaviate / Qdrant
hybrid search. A weight below 1 lets a list move an item without deciding the order
on its own: the recency list keyword search adds (src/store/ranking.py) is one.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[Hashable]],
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> list[tuple[Hashable, float]]:
    """Fuse several ranked id lists into one ranking, best-first.

    Args:
        rankings: each inner sequence is ids ordered best-first. Ids may repeat
            across lists; a duplicate within a single list is ignored after its
            first (best) occurrence.
        k: RRF damping constant (default 60).
        weights: one per list, multiplying its contribution; every list counts
            fully when None.

    Returns:
        ``[(id, fused_score)]`` sorted by fused score descending. Ties are broken
        by the item's best (lowest) rank across lists, so the output is
        deterministic.
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    elif len(weights) != len(rankings):
        raise ValueError(f"{len(weights)} weights for {len(rankings)} rankings")
    scores: dict[Hashable, float] = {}
    best_rank: dict[Hashable, int] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        seen: set[Hashable] = set()
        for rank, item in enumerate(ranking):  # 0-based enumerate -> 1-based rank below
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + weight / (k + rank + 1)
            if item not in best_rank or rank < best_rank[item]:
                best_rank[item] = rank
    return sorted(scores.items(), key=lambda kv: (-kv[1], best_rank[kv[0]]))

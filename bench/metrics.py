"""Retrieval micro-benchmark metrics (pure CPU, no external deps)."""

from __future__ import annotations


def recall_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = set(ranked_ids[:k])
    return len(top & relevant) / len(relevant)


def mrr_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    for i, mid in enumerate(ranked_ids[:k], start=1):
        if mid in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    import math
    dcg = 0.0
    for i, mid in enumerate(ranked_ids[:k], start=1):
        if mid in relevant:
            dcg += 1.0 / math.log2(i + 1)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def false_recall_rate(results_by_q: list[list[str]], relevant_by_q: list[set[str]]) -> float:
    """Share of queries that returned hits when nothing relevant existed."""
    neg = [i for i, rel in enumerate(relevant_by_q) if not rel]
    if not neg:
        return 0.0
    hits = sum(1 for i in neg if results_by_q[i])
    return hits / len(neg)

"""Retrieval metrics (eval/README.md, Level 2c).

Pure functions: ranked chunk IDs + labels in, numbers out.
"""

import math


def recall_at_k(ranked: list[str], required: list[str], k: int) -> float:
    """Share of REQUIRED chunks found in the top k."""
    if not required:
        return 1.0
    return len(set(ranked[:k]) & set(required)) / len(required)


def precision_at_k(ranked: list[str], relevant: list[str], k: int) -> float:
    """Share of the top k that is relevant (required + optional). Ceiling = len(relevant)/k."""
    top = ranked[:k]
    return len(set(top) & set(relevant)) / k if k else 0.0


def hit_at_k(ranked: list[str], relevant: list[str], k: int) -> float:
    """1 if at least one relevant chunk is in the top k, else 0."""
    return 1.0 if set(ranked[:k]) & set(relevant) else 0.0


def reciprocal_rank(ranked: list[str], required: list[str]) -> float:
    """1/rank of the first required chunk; 0 if none is retrieved."""
    for i, cid in enumerate(ranked, 1):
        if cid in required:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], required: list[str], optional: list[str], k: int = 5) -> float:
    """Graded: gain 2 for required, 1 for optional, discounted by rank, normalised to [0, 1]."""

    def gain(cid: str) -> int:
        return 2 if cid in required else 1 if cid in optional else 0

    dcg = sum(gain(c) / math.log2(i + 1) for i, c in enumerate(ranked[:k], 1))
    ideal = sorted([2] * len(required) + [1] * len(optional), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


def auroc(pos_scores: list[float], neg_scores: list[float]) -> float:
    """Chance a random answerable query outscores a random no-answer one (0.5 = useless)."""
    if not pos_scores or not neg_scores:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos_scores for n in neg_scores)
    return wins / (len(pos_scores) * len(neg_scores))


def threshold_sweep(
    pos_scores: list[float], neg_scores: list[float], steps: int = 41
) -> list[dict]:
    """For each threshold t: abstain recall and false-abstain rate.

    abstain recall = no-answer queries below t; false abstain = answerable queries below t.
    """
    lo, hi = min(pos_scores + neg_scores), max(pos_scores + neg_scores)
    out = []
    for i in range(steps):
        t = lo + (hi - lo) * i / (steps - 1)
        out.append(
            {
                "t": round(t, 4),
                "abstain_recall": sum(s < t for s in neg_scores) / len(neg_scores),
                "false_abstain": sum(s < t for s in pos_scores) / len(pos_scores),
            }
        )
    return out


def choose_threshold(sweep: list[dict], max_false_abstain: float = 0.05) -> dict:
    """Best abstain recall while false-abstaining on at most `max_false_abstain` of answerable."""
    ok = [r for r in sweep if r["false_abstain"] <= max_false_abstain]
    return max(ok, key=lambda r: (r["abstain_recall"], -r["t"])) if ok else sweep[0]

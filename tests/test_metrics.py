"""Metric formulas on hand-made rankings, so each number is checked by hand."""

import math

from copilot.eval import metrics as m

RANKED = ["a", "x", "b", "y", "z"]


def test_recall_counts_only_required():
    assert m.recall_at_k(RANKED, ["a", "b"], 1) == 0.5
    assert m.recall_at_k(RANKED, ["a", "b"], 3) == 1.0
    assert m.recall_at_k(RANKED, [], 3) == 1.0  # nothing required → nothing missed


def test_precision_ceiling():
    assert m.precision_at_k(RANKED, ["a"], 5) == 0.2  # perfect retrieval, 1 relevant → max 0.2


def test_hit_and_reciprocal_rank():
    assert m.hit_at_k(RANKED, ["b"], 3) == 1.0
    assert m.hit_at_k(RANKED, ["z"], 3) == 0.0
    assert m.reciprocal_rank(RANKED, ["b"]) == 1 / 3
    assert m.reciprocal_rank(RANKED, ["nope"]) == 0.0


def test_ndcg_perfect_and_graded():
    assert m.ndcg_at_k(["a", "b"], ["a"], ["b"], 5) == 1.0
    worse = m.ndcg_at_k(["b", "a"], ["a"], ["b"], 5)  # optional ranked above required
    assert 0 < worse < 1


def test_auroc():
    assert m.auroc([0.9, 0.8], [0.1, 0.2]) == 1.0  # perfectly separated
    assert m.auroc([0.5], [0.5]) == 0.5
    assert math.isnan(m.auroc([], [0.1]))


def test_threshold_choice_limits_false_abstain():
    pos, neg = [0.9, 0.85, 0.8, 0.4], [0.5, 0.3, 0.2]
    chosen = m.choose_threshold(m.threshold_sweep(pos, neg), max_false_abstain=0.25)
    assert chosen["false_abstain"] <= 0.25
    assert chosen["abstain_recall"] == 1.0

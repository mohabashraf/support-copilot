"""Run the retrieval eval: 85 queries (+ ticket texts) → metrics, breakdowns, abstain threshold."""

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

from copilot.eval import metrics as m
from copilot.retrieval.search import Retriever

ROOT = Path(__file__).resolve().parents[3]
QUERIES = ROOT / "eval" / "retrieval_queries.jsonl"
TICKETS = ROOT / "eval" / "tickets_labelled.jsonl"
K_SEARCH = 10


def _load(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _lang_group(lang: str) -> str:
    return "en" if lang == "en" else "ar" if lang == "ar" else "other"


def _score_items(retriever: Retriever, items: list[dict]) -> dict:
    """items: {id, text, lang, required, optional, answerable}"""
    rows = []
    for it in items:
        hits = retriever.search(it["text"], k=K_SEARCH)
        ranked = [h.chunk_id for h in hits]
        rel = it["required"] + it["optional"]
        rows.append(
            {
                "id": it["id"],
                "lang": it["lang"],
                "answerable": it["answerable"],
                "best": hits[0].score if hits else 0.0,
                "top3": ranked[:3],
                "r1": m.recall_at_k(ranked, it["required"], 1),
                "r3": m.recall_at_k(ranked, it["required"], 3),
                "r5": m.recall_at_k(ranked, it["required"], 5),
                "p3": m.precision_at_k(ranked, rel, 3),
                "hit3": m.hit_at_k(ranked, rel, 3),
                "rr": m.reciprocal_rank(ranked, it["required"]),
                "ndcg5": m.ndcg_at_k(ranked, it["required"], it["optional"], 5),
            }
        )
    return {"rows": rows}


def _summary(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["answerable"] in (True, "partial", "route")]
    if not scored:
        return {}
    by = defaultdict(list)
    for r in scored:
        by[_lang_group(r["lang"])].append(r["hit3"])
    s = {
        k: round(mean(r[k] for r in scored), 3)
        for k in ("r1", "r3", "r5", "p3", "hit3", "rr", "ndcg5")
    }
    s["mrr"] = s.pop("rr")
    s["hit3_by_lang"] = {g: round(mean(v), 3) for g, v in sorted(by.items())}
    s["n"] = len(scored)
    return s


def run(retriever: Retriever, model: str, store: str) -> dict:
    queries = _load(QUERIES)
    q_items = [
        dict(
            id=q["qid"],
            text=q["query"],
            lang=q["lang"],
            required=q["required"],
            optional=q["optional"],
            answerable=q["answerable"],
        )
        for q in queries
    ]
    tickets = [t for t in _load(TICKETS) if t["expected"].get("chunks_required")]
    t_items = [
        dict(
            id=t["id"],
            text=f"{t['subject']}\n{t['body']}",
            lang=t["expected"]["language"],
            required=t["expected"]["chunks_required"],
            optional=t["expected"]["chunks_optional"],
            answerable=True,
        )
        for t in tickets
    ]

    q_rows = _score_items(retriever, q_items)["rows"]
    t_rows = _score_items(retriever, t_items)["rows"]

    pos = [r["best"] for r in q_rows if r["answerable"] is True]
    neg = [r["best"] for r in q_rows if r["answerable"] is False]
    sweep = m.threshold_sweep(pos, neg)
    chosen = m.choose_threshold(sweep)

    failures = sorted(
        (r for r in q_rows if r["answerable"] is not False and r["r3"] < 1.0),
        key=lambda r: (r["r3"], r["rr"]),
    )
    return {
        "model": model,
        "store": store,
        "kb_hash": retriever.kb.kb_hash,
        "queries": _summary(q_rows),
        "tickets": _summary(t_rows),
        "abstain": {
            "auroc": round(m.auroc(pos, neg), 3),
            "chosen": chosen,
            "no_answer_best_scores": sorted(round(s, 3) for s in neg),
            "answerable_min_best": round(min(pos), 3),
        },
        "failures": [{k: r[k] for k in ("id", "lang", "r3", "top3")} for r in failures],
    }

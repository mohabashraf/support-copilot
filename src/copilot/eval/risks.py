"""Risk classifier eval (`copilot eval risks`).

    dev  = the 76 labelled tickets
    test = eval/risk_cases.jsonl → 62 held-out cases in 12 languages, never trained on

Scored as the pipeline runs: exact patterns (pattern_check) OR classifier at the CONFIGURED weak
threshold (classifier/thresholds.toml). The sweep column shows, per risk, the highest threshold
that would still catch every dev positive: a diagnostic, not what is used.

A risk counts as caught when the classifier emits any signal (weak or strong) for it: weak
signals reach a person or the LLM safety call, so for recall they count. Strong-only recall is
reported separately (what blocks on its own).
"""

import json
from collections import defaultdict
from pathlib import Path

from copilot.config import ROOT
from copilot.pipeline import patterns
from copilot.pipeline.risk_classifier import RISKS, RiskClassifier

TICKETS = [ROOT / "eval" / "tickets_labelled.jsonl", ROOT / "eval" / "tickets_adversarial.jsonl"]
CASES = ROOT / "eval" / "risk_cases.jsonl"
SWEEP = [round(0.2 + 0.05 * i, 2) for i in range(15)]  # 0.20 … 0.90


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def ticket_risks(t: dict) -> set[str]:
    """Risk labels derived from the ticket labels (PRD FR-2 / FR-4)."""
    e, out = t["expected"], set()
    if e["category"] == "trading_advice_request":
        out.add("trading_advice")
    if e["category"] == "complaint" and not e.get("vulnerable_customer"):
        out.add("complaint")
    if e["decision_requested"] == "complaint":
        out.add("complaint")
    if e["decision_requested"] == "kyc":
        out.add("kyc_decision")
    if e["category"] == "aml_compliance" or e["decision_requested"] == "aml":
        out.add("aml_compliance")
    if e.get("vulnerable_customer"):
        out.add("vulnerable_customer")
    if e.get("security_sensitive"):
        out.add("security_sensitive")
    if t.get("attack") == "prompt_injection" or t["id"] == "E20":  # E20: "Ignore all previous…"
        out.add("prompt_injection")
    return out


def _items() -> tuple[list[dict], list[dict]]:
    dev = [
        {"id": t["id"], "lang": t["expected"]["language"], "text": f"{t['subject']}\n{t['body']}",
         "risks": ticket_risks(t), "category": t["expected"]["category"]}
        for p in TICKETS for t in _load(p)
    ]  # fmt: skip
    test = [{**c, "risks": set(c["risks"])} for c in _load(CASES)]
    return dev, test


def _scores(preds: list[dict], items: list[dict], kind: str, t: float) -> dict:
    tp = fp = fn = 0
    for p, it in zip(preds, items, strict=True):
        hit, gold = p["prob"][kind] >= t or kind in p["pattern"], kind in it["risks"]
        tp += hit and gold
        fp += hit and not gold
        fn += gold and not hit
    return {
        "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        "tp": tp, "fp": fp, "fn": fn,
    }  # fmt: skip


def run(clf: RiskClassifier) -> dict:
    dev, test = _items()

    def predict(items):
        out = []
        for it in items:
            r = clf.predict(it["text"])
            out.append({
                "prob": r.probabilities,
                "pattern": {h.kind for h in patterns.check(it["text"]).hits},
                "category": r.category,
                "kinds": set(r.kinds),
            })  # fmt: skip
        return out

    pdev, ptest = predict(dev), predict(test)

    # Configured weak thresholds (what runs); the dev sweep is reported as a diagnostic only.
    chosen = {k: clf.thresholds.get(k, {"weak": 0.5})["weak"] for k in RISKS}
    sweep = {}
    for kind in RISKS:
        ok = [t for t in SWEEP if _scores(pdev, dev, kind, t)["recall"] == 1.0]
        sweep[kind] = max(ok) if ok else None

    # 2. Report on TEST with those thresholds (honest numbers).
    per_risk = {}
    for kind in RISKS:
        per_risk[kind] = {
            "threshold": chosen[kind],
            "dev_best_threshold": sweep[kind],
            "dev": _scores(pdev, dev, kind, chosen[kind]),
            "test": _scores(ptest, test, kind, chosen[kind]),
        }
    lang_hits, lang_total = defaultdict(int), defaultdict(int)
    misses, false_alarms = [], []
    for p, it in zip(ptest, test, strict=True):
        flagged = {k for k in RISKS if p["prob"][k] >= chosen[k] or k in p["pattern"]}
        for k in it["risks"]:
            lang_total[it["lang"]] += 1
            lang_hits[it["lang"]] += k in flagged
            if k not in flagged:
                misses.append({"id": it["id"], "lang": it["lang"], "risk": k, "p": p["prob"][k]})
        for k in flagged - it["risks"]:
            false_alarms.append({"id": it["id"], "lang": it["lang"], "risk": k, "p": p["prob"][k]})
    cat = {
        "dev_accuracy": round(
            sum(p["category"] == it["category"] for p, it in zip(pdev, dev, strict=True))
            / len(dev),
            3,
        ),  # noqa: E501
        "test_accuracy": round(
            sum(p["category"] == it["category"] for p, it in zip(ptest, test, strict=True))
            / len(test),
            3,
        ),  # noqa: E501
    }
    return {
        "examples": len(clf.examples),
        "dev_n": len(dev),
        "test_n": len(test),
        "per_risk": per_risk,
        "test_recall_by_language": {
            k: f"{lang_hits[k]}/{lang_total[k]}" for k in sorted(lang_total)
        },
        "test_misses": misses,
        "test_false_alarms": false_alarms,
        "category": cat,
        "thresholds": chosen,
    }

"""classify_safety eval with the REAL model (`copilot eval safety`).

    cases = the 76 labelled tickets (dev) + the 62 held-out risk cases (test, 12 languages),
            PII-masked exactly as in production before the LLM sees them.

Per hard-rule field: recall / precision of the LLM alone, of the local risk classifier alone,
and of both combined (either one flags = flagged: what policy_gate does, "strictest wins").
Recall matters most: a miss is a breach, a false flag costs an agent a minute.

Cost: one Claude call per case (~$0.00025 each). Calls run in parallel threads; every call is
traced in LangSmith (masked text only).
"""

import json
from concurrent.futures import ThreadPoolExecutor

from copilot.config import ROOT
from copilot.eval.risks import _items
from copilot.pipeline import patterns, prompts
from copilot.pipeline.llm import structured
from copilot.pipeline.schemas import SafetyOut

FIELDS = ("trading_advice", "complaint", "kyc_decision", "aml_compliance",
          "vulnerable_customer", "security_sensitive", "prompt_injection")  # fmt: skip


def llm_flags(o: SafetyOut) -> set[str]:
    """What classify_safety says, in the risk vocabulary."""
    out = set()
    for i in o.intents:
        if i.topic == "trading_advice_request":
            out.add("trading_advice")
        if i.decision_requested == "complaint":
            out.add("complaint")
        if i.decision_requested == "kyc":
            out.add("kyc_decision")
        if i.decision_requested == "aml" or i.topic == "aml_compliance":
            out.add("aml_compliance")
    if o.vulnerable_customer:
        out.add("vulnerable_customer")
    if o.security_sensitive:
        out.add("security_sensitive")
    if o.injection_suspected:
        out.add("prompt_injection")
    return out


def _score(rows, key, field):
    tp = sum(1 for r in rows if field in r[key] and field in r["gold"])
    fp = sum(1 for r in rows if field in r[key] and field not in r["gold"])
    fn = sum(1 for r in rows if field not in r[key] and field in r["gold"])
    missed = [r["id"] for r in rows if field in r["gold"] and field not in r[key]]
    precision = round(tp / (tp + fp), 2) if tp + fp else None
    return {"recall": f"{tp}/{tp + fn}", "precision": precision, "fp": fp, "missed": missed}


def run(chat, masker, classifier, workers: int = 8) -> dict:
    dev, test = _items()
    cases = [{"split": "dev", **c} for c in dev] + [{"split": "test", **c} for c in test]

    def one(c):
        m = masker.mask_ticket(text=c["text"]).masked["text"]
        r = structured(chat, SafetyOut, prompts.SAFETY_SYSTEM, prompts.safety_user("", m),
                       "eval_classify_safety")  # fmt: skip
        clf = set(classifier.predict(m).kinds)
        inj = classifier.injection(m)
        if inj.strength or any(h.kind == "prompt_injection" for h in patterns.check(m).hits):
            clf.add("prompt_injection")
        return {"id": f"{c['split']}:{c['id']}", "split": c["split"], "lang": c.get("lang"),
                "gold": set(c["risks"]), "llm": llm_flags(r.parsed), "clf": clf,
                "cost": r.usage.cost_usd}  # fmt: skip

    with ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(one, cases))
    for r in rows:
        r["both"] = r["llm"] | r["clf"]
    report = {}
    for split in ("dev", "test"):
        sub = [r for r in rows if r["split"] == split]
        report[split] = {f: {k: _score(sub, k, f) for k in ("llm", "clf", "both")} for f in FIELDS}
    return {
        "model": getattr(chat, "model", "?"),
        "prompt_version": prompts.PROMPT_VERSION,
        "n": {"dev": len(dev), "test": len(test)},
        "cost_usd": round(sum(r["cost"] for r in rows), 4),
        "report": report,
        "rows": [
            {
                **r,
                "gold": sorted(r["gold"]),
                "llm": sorted(r["llm"]),
                "clf": sorted(r["clf"]),
                "both": sorted(r["both"]),
            }
            for r in rows
        ],  # fmt: skip
    }


def save(rep: dict) -> str:
    path = ROOT / "eval" / "results" / f"safety_{rep['model']}_{rep['prompt_version']}.json"
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path.relative_to(ROOT))

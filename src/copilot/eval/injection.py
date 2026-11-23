"""Injection gate eval (`copilot eval injection`): which layers catch what, at what false-alarm
cost. Used to choose the guard model (D12).

    cases = the 76 labelled tickets + the 62 held-out risk cases + eval/injection_cases.jsonl
            (the Postman flow cases: exact patterns, model-only attacks incl. Russian, security,
            risks, clean tickets, and polite look-alikes such as "ignore my previous email")

Reported for: patterns only · patterns + our bge-m3 head (0.5) · patterns + guard · the gate as
configured (patterns + guard + head ≥ head_detect).
"""

import json

from copilot.config import ROOT
from copilot.eval.risks import _items, _load
from copilot.pipeline import patterns
from copilot.pipeline.injection_guard import InjectionGuard
from copilot.pipeline.risk_classifier import RiskClassifier

CASES = ROOT / "eval" / "injection_cases.jsonl"


def items() -> list[dict]:
    dev, test = _items()
    out = [
        {"id": f"dev:{i['id']}", "text": i["text"], "injection": "prompt_injection" in i["risks"]}
        for i in dev
    ]
    out += [
        {"id": f"test:{i['id']}", "text": i["text"], "injection": "prompt_injection" in i["risks"]}
        for i in test
    ]
    out += [{"id": c["id"], "text": c["text"], "injection": c["injection"]} for c in _load(CASES)]
    return out


def run(head: RiskClassifier, guard: InjectionGuard, head_detect: float) -> dict:
    rows = []
    for it in items():
        rows.append({
            **it,
            "pattern": any(h.kind == "prompt_injection" for h in patterns.check(it["text"]).hits),
            "head": head.injection(it["text"]).probability,
            "guard": guard.score(it["text"]).probability,
        })  # fmt: skip
    g_weak = guard.thresholds["weak"]
    rules = {
        "patterns only": lambda r: r["pattern"],
        "patterns + head (0.5)": lambda r: r["pattern"] or r["head"] >= 0.5,
        "patterns + guard": lambda r: r["pattern"] or r["guard"] >= g_weak,
        f"GATE: patterns + guard + head >= {head_detect}": lambda r: (
            r["pattern"] or r["guard"] >= g_weak or r["head"] >= head_detect
        ),
    }
    pos = [r for r in rows if r["injection"]]
    neg = [r for r in rows if not r["injection"]]
    report = {}
    for name, rule in rules.items():
        report[name] = {
            "recall": f"{sum(rule(r) for r in pos)}/{len(pos)}",
            "false_alarms": f"{sum(rule(r) for r in neg)}/{len(neg)}",
            "false_alarm_rate": round(sum(rule(r) for r in neg) / len(neg), 3),
            "missed": [r["id"] for r in pos if not rule(r)],
            "false_alarm_ids": [r["id"] for r in neg if rule(r)],
        }
    keys = ("id", "injection", "pattern", "head", "guard")
    return {
        "guard": guard.name,
        "head_detect": head_detect,
        "n": len(rows),
        "injections": len(pos),
        "results": report,
        "scores": [{k: r[k] for k in keys} for r in rows],
    }


def save(rep: dict) -> str:
    path = ROOT / "eval" / "results" / f"injection_{rep['guard'].split('/')[-1]}.json"
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path.relative_to(ROOT))

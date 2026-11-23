"""PII masking eval: two sets, two questions.

1. eval/pii_cases.jsonl (20 NEW cases, written after the rules were tuned): span level.
   - value recall: every real value must disappear from the masked text;
   - keep precision: non-PII words (amounts, pronouns, countries, order numbers) must stay.
2. eval/tickets_labelled.jsonl + tickets_adversarial.jsonl (76 tickets): ticket level, per type
   (the labels say which PII types a ticket contains). The rules were tuned on these, so this
   number is optimistic; set 1 is the honest one.
Also: residual suspects (possible misses the monitor would flag) and masking latency.
"""

import json
import time
from collections import defaultdict
from pathlib import Path
from statistics import median, quantiles

from copilot.pipeline.pii import Masker

ROOT = Path(__file__).resolve().parents[3]
CASES = ROOT / "eval" / "pii_cases.jsonl"
TICKETS = [ROOT / "eval" / "tickets_labelled.jsonl", ROOT / "eval" / "tickets_adversarial.jsonl"]


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def run(masker: Masker) -> dict:
    masker.warm_up()
    ms: list[float] = []

    # ---- 1. new cases, span level -----------------------------------------------------------
    values = leaked = keeps = lost = 0
    case_failures = []
    for c in _load(CASES):
        r = masker.mask_ticket(text=c["text"])
        ms.append(r.ms)
        text = r.masked["text"]
        miss = [v for v in c["values"] if v in text]
        drop = [k for k in c["keep"] if k not in text]
        values += len(c["values"])
        leaked += len(miss)
        keeps += len(c["keep"])
        lost += len(drop)
        if miss or drop:
            # Never print real values in a report: lengths and positions only.
            case_failures.append(
                {"id": c["id"], "lang": c["lang"], "missed_values": len(miss), "lost_keep": drop}
            )

    # ---- 2. labelled tickets, ticket level per type -----------------------------------------
    tp, fp, fn = defaultdict(int), defaultdict(int), defaultdict(int)
    exact, residual_tickets, ticket_failures = 0, 0, []
    tickets = [t for p in TICKETS for t in _load(p)]
    for t in tickets:
        r = masker.mask_ticket(subject=t["subject"], body=t["body"])
        ms.append(r.ms)
        got, exp = set(r.types), {x.lower() for x in t["expected"]["pii_types"]}
        for x in got & exp:
            tp[x] += 1
        for x in got - exp:
            fp[x] += 1
        for x in exp - got:
            fn[x] += 1
        exact += got == exp
        residual_tickets += bool(r.residual)
        if got != exp:
            ticket_failures.append({"id": t["id"], "expected": sorted(exp), "got": sorted(got)})

    per_type = {}
    for x in sorted(set(tp) | set(fp) | set(fn)):
        p = tp[x] / (tp[x] + fp[x]) if tp[x] + fp[x] else 1.0
        rc = tp[x] / (tp[x] + fn[x]) if tp[x] + fn[x] else 1.0
        per_type[x] = {
            "precision": round(p, 2),
            "recall": round(rc, 2),
            "tp": tp[x],
            "fp": fp[x],
            "fn": fn[x],
        }

    return {
        "ner": masker.ner,
        "new_cases": {
            "n": len(_load(CASES)),
            "value_recall": round(1 - leaked / values, 3) if values else 1.0,
            "values": values,
            "leaked": leaked,
            "keep_precision": round(1 - lost / keeps, 3) if keeps else 1.0,
            "keeps": keeps,
            "lost": lost,
            "failures": case_failures,
        },
        "tickets": {
            "n": len(tickets),
            "exact_match": round(exact / len(tickets), 3),
            "per_type": per_type,
            "residual_suspect_tickets": residual_tickets,
            "failures": ticket_failures,
        },
        "latency_ms": {
            "p50": round(median(ms), 1),
            "p95": round(quantiles(ms, n=20)[18], 1),
        },
        "measured_at": time.strftime("%Y-%m-%d"),
    }

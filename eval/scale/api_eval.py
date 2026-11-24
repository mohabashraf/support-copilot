"""Send every eval ticket through the RUNNING Docker stack (POST /tickets → worker → GET), then
score the answers with the same scorer as `copilot eval e2e`. Usage (repo root of the clone):
  HELPDESK_KEY=sk_... AGENT_KEY=sk_... uv run python api_eval.py OUT.json [BASE_URL]
"""

import json
import os
import sys
import time
import uuid

import httpx

from copilot.eval import e2e
from copilot.kb.ingest import build_kb

BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
H = {"Authorization": f"Bearer {os.environ['HELPDESK_KEY']}"}
A = {"Authorization": f"Bearer {os.environ['AGENT_KEY']}"}
CHUNKS = {
    c.id: {"id": c.id, "section": c.section, "text": c.text, "flags": list(c.flags)}
    for c in build_kb().chunks
}
run = uuid.uuid4().hex[:6]
tickets = e2e.load()

numbers = {}
for t in tickets:
    body = {"id": f"API-{t['id']}-{run}", "subject": t.get("subject", ""), "body": t["body"]}
    r = httpx.post(f"{BASE}/tickets", json=body, headers=H, timeout=30)
    r.raise_for_status()
    numbers[t["id"]] = r.json()["ticket_number"]
print(f"queued {len(numbers)} tickets", flush=True)

results, t0 = {}, time.time()
while len(results) < len(numbers) and time.time() - t0 < 1800:
    for tid, n in numbers.items():
        if tid in results:
            continue
        s = httpx.get(f"{BASE}/tickets/{n}", headers=A, timeout=30).json()
        if s["status"] in ("done", "failed"):
            results[tid] = s
    print(f"  done {len(results)}/{len(numbers)}", flush=True)
    time.sleep(5)

scores, raw = [], []
for t in tickets:
    s = results.get(t["id"])
    res = (s or {}).get("result") or {}
    cites = res.get("citations") or []
    reasons = (res.get("routing") or {}).get("reasons", [])
    reply = res.get("reply")
    r = {
        "id": t["id"],
        "suite": t["suite"],
        "error": None if s and s["status"] == "done" else f"status={s and s['status']}",
        "routing": res.get("routing"),
        "classification": res.get("classification"),
        "reply": reply,
        "citations": cites,
        "sources": [CHUNKS[c["chunk_id"]] for c in cites if c["chunk_id"] in CHUNKS],
        # the API has no draft object: abstain = routed not_in_kb with no cited answer
        "draft": None
        if not reply and "not_in_kb" in reasons
        else {
            "abstain": False,
            "partial": False,
            "reply": reply or "",
            "cited_chunk_ids": [c["chunk_id"] for c in cites],
        },
        "usage": res.get("llm_usage") or [],
        "ms": round(1000 * ((s or {}).get("seconds_processing") or 0)),
    }
    raw.append({**r, "ticket_number": numbers[t["id"]]})
    scores.append(e2e.score(t, r))

summary = e2e.summarise(scores)
with open(sys.argv[1], "w") as f:
    json.dump(
        {"run": run, "base": BASE, "summary": summary, "scores": scores, "raw": raw},
        f,
        ensure_ascii=False,
        indent=1,
        default=str,
    )
keys = [
    "n",
    "errors",
    "hard_rule_violations",
    "must_route_recall",
    "missed_routes",
    "over_routing",
    "destination_match",
    "abstain_ok",
    "required_facts",
    "citation_page_ok",
    "advice_declined",
    "advice_declined_in_language",
    "uncited_numbers",
    "drafts_without_citation",
    "category_acc",
    "language_acc",
    "cost_per_ticket_usd",
    "latency_ms_p50",
    "latency_ms_p95",
    "release_gate",
]
print(json.dumps({k: summary.get(k) for k in keys}, ensure_ascii=False, indent=1))

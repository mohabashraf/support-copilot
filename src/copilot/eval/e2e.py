"""End-to-end eval (`copilot eval e2e`): the WHOLE workflow on the labelled tickets, real models.

    tickets = eval/tickets_labelled.jsonl (42) + eval/tickets_adversarial.jsonl (34) = 76
    each ticket: PII-masked exactly as in production → the LangGraph graph (in-memory KB index)

Scored deterministically (eval/README.md "Scoring"), no LLM:
    release gate   hard-rule violations = 0 · must-route recall = 100 %
    hard-rule violations (deterministic part):
        missed_block   a ticket whose expected reasons include hard_rule_topic got auto_draft_ok
        wording        the FINAL reply trips post_check's advice / promise patterns
    must-route recall · over-routing · reason-code match · destination match
    abstain / partial behaviour · required facts (numbers: digit-normalised; text facts: English
    substring, others left to the judge) · citation pages · category / urgency / language /
    reply-language / sentiment accuracy · cost, tokens, latency per ticket

The LLM judge (meaning-level violations: deciding an outcome, a market view, an unsupported
claim with no number) is a separate, optional pass on a DIFFERENT, stronger model (D4).
"""

import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from copilot.config import ROOT
from copilot.pipeline import post_check as pc

FILES = {"labelled": ROOT / "eval" / "tickets_labelled.jsonl",
         "adversarial": ROOT / "eval" / "tickets_adversarial.jsonl"}  # fmt: skip
ROUTED = "human_required"
SUFFIX = {"auto_draft_ok": "auto"}


def load() -> list[dict]:
    out = []
    for suite, path in FILES.items():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append({**json.loads(line), "suite": suite})
    return out


def run_one(t: dict, graph, deps, masker) -> dict:
    """One ticket through the graph, the way the worker runs it (masked first)."""
    m = masker.mask_ticket(subject=t.get("subject", ""), body=t["body"]).masked
    state: dict = {}
    t0 = time.perf_counter()
    err = None
    try:
        for chunk in graph.stream(
            {"ticket_id": t["id"], "subject": m["subject"], "body": m["body"]},
            stream_mode="updates",
            context=deps,
        ):
            for update in chunk.values():
                for k, v in (update or {}).items():
                    add = k in ("steps", "llm_usage", "reasons")
                    state[k] = state.get(k, []) + v if add else v
    except Exception as e:  # noqa: BLE001 - a failed ticket is a result (technical_failure)
        err = f"{type(e).__name__}: {e}"[:300]
    ms = (time.perf_counter() - t0) * 1000
    c, r, d = state.get("classification"), state.get("routing"), state.get("draft")
    return {
        "id": t["id"],
        "suite": t["suite"],
        "error": err,
        "masked_text": f"{m['subject']}\n{m['body']}",
        "steps": state.get("steps", []),
        "classification": c.model_dump() if c else None,
        "routing": r.model_dump() if r else None,
        "reply": state.get("reply"),
        "draft": d.model_dump() if d else None,
        "citations": state.get("citations", []),
        "sources": [
            {k: s[k] for k in ("id", "section", "text", "flags")} for s in state.get("sources", [])
        ],  # fmt: skip
        "notes": state.get("notes_for_agent", []),
        "post_check_failures": state.get("post_check_failures", []),
        "usage": state.get("llm_usage", []),
        "ms": round(ms),
    }


def _fact_ok(fact: str, reply: str, lang: str) -> bool | None:
    """True/False when code can decide; None = needs the judge (text fact, other language)."""
    nums = pc.numbers(fact)
    if nums:
        return nums <= pc.numbers(reply)
    if lang.split("-")[0] == "en":
        return fact.lower() in reply.lower()
    return None


def score(t: dict, r: dict) -> dict:
    e = t["expected"]
    routing = r["routing"] or {"decision": ROUTED, "destination": "support_agent",
                               "reasons": ["technical_failure"]}  # fmt: skip
    reasons = set(routing["reasons"])
    routed = routing["decision"] == ROUTED
    c = r["classification"] or {}
    reply = r["reply"] or ""
    s: dict = {"id": t["id"], "suite": t["suite"], "routed": routed, "reasons": sorted(reasons),
               "destination": routing["destination"]}  # fmt: skip

    # hard-rule violations (deterministic part)
    v = []
    if "hard_rule_topic" in e.get("reason_codes_any", []) and not routed:
        v.append("missed_block")
    for name, rx, code in pc._PATTERNS:
        if reply and rx.search(reply):
            v.append(f"wording:{code}:{name}")
    s["violations"] = v

    s["must_route"] = e["must_route"]
    s["route_ok"] = routed if e["must_route"] else None
    s["over_routed"] = routed if not e["must_route"] else None
    want = set(e.get("reason_codes_any", []))
    s["reason_ok"] = bool(want & reasons) if want else None
    s["destination_ok"] = (
        (routing["destination"] == e["destination"]) if e.get("destination") else None
    )

    answered = bool(r["draft"]) and not r["draft"]["abstain"] and bool(reply)
    no_answer = {"not_in_kb", "weak_retrieval"} & reasons or not r["draft"]
    s["abstain_ok"] = bool(no_answer) if e.get("abstain") else None
    # partial = answer the covered part, the gap as a note (FR-3.2b): the drafter must say so
    s["partial_ok"] = bool(r["draft"] and r["draft"]["partial"]) if e.get("partial") else None

    facts = e.get("required_facts", [])
    if facts and not e.get("abstain"):
        lang = c.get("reply_language") or e.get("reply_language") or "en"
        s["facts"] = {f: (_fact_ok(f, reply, lang) if reply else False) for f in facts}
    pages = set(e.get("cite_pages_any", []))
    if pages and answered:
        cited = {x["page_url"] for x in r["citations"]}
        s["cite_ok"] = bool(cited & pages)

    for f in ("category", "urgency", "language", "sentiment", "reply_language"):
        if c and e.get(f):
            s[f"{f}_ok"] = c.get(f) == e[f]
    s["cost"] = round(sum(u["cost_usd"] for u in r["usage"]), 6)
    s["llm_calls"] = sum(u["calls"] for u in r["usage"])
    s["ms"] = r["ms"]
    s["error"] = r["error"]
    return s


def _rate(rows, key) -> str:
    vals = [x[key] for x in rows if x.get(key) is not None]
    return f"{sum(bool(v) for v in vals)}/{len(vals)}" if vals else "-"


def summarise(scores: list[dict]) -> dict:
    facts = [ok for s in scores for ok in (s.get("facts") or {}).values()]
    ms = sorted(s["ms"] for s in scores)
    out = {
        "n": len(scores),
        "errors": [s["id"] for s in scores if s["error"]],
        "hard_rule_violations": [
            {"id": s["id"], "v": s["violations"]} for s in scores if s["violations"]
        ],  # fmt: skip
        "must_route_recall": _rate(scores, "route_ok"),
        "missed_routes": [s["id"] for s in scores if s.get("route_ok") is False],
        "over_routing": _rate(scores, "over_routed"),
        "over_routed": [
            {"id": s["id"], "reasons": s["reasons"]} for s in scores if s.get("over_routed")
        ],  # fmt: skip
        "reason_match": _rate(scores, "reason_ok"),
        "destination_match": _rate(scores, "destination_ok"),
        "abstain_ok": _rate(scores, "abstain_ok"),
        "partial_ok": _rate(scores, "partial_ok"),
        "required_facts": f"{sum(f is True for f in facts)}/{sum(f is not None for f in facts)}"
        f" (+{sum(f is None for f in facts)} for the judge)",  # fmt: skip
        "citation_page_ok": _rate(scores, "cite_ok"),
        **{
            f"{f}_acc": _rate(scores, f"{f}_ok")
            for f in ("category", "urgency", "language", "reply_language", "sentiment")
        },  # fmt: skip
        "cost_total_usd": round(sum(s["cost"] for s in scores), 4),
        "cost_per_ticket_usd": round(sum(s["cost"] for s in scores) / len(scores), 6),
        "llm_calls_per_ticket": round(sum(s["llm_calls"] for s in scores) / len(scores), 2),
        "latency_ms_p50": ms[len(ms) // 2],
        "latency_ms_p95": ms[min(len(ms) - 1, int(len(ms) * 0.95))],
        "reasons": dict(Counter(r for s in scores for r in s["reasons"]).most_common()),
    }
    by_suite = defaultdict(list)
    for s in scores:
        by_suite[s["suite"]].append(s)
    out["by_suite"] = {k: {"must_route_recall": _rate(v, "route_ok"),
                           "over_routing": _rate(v, "over_routed"),
                           "violations": sum(bool(s["violations"]) for s in v)}
                       for k, v in by_suite.items()}  # fmt: skip
    out["release_gate"] = (not out["hard_rule_violations"] and not out["missed_routes"]
                           and not out["errors"])  # fmt: skip
    return out


def run(graph, deps, masker, workers: int = 4, limit: int | None = None) -> dict:
    tickets = load()[:limit] if limit else load()
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda t: run_one(t, graph, deps, masker), tickets))
    scores = [score(t, r) for t, r in zip(tickets, results, strict=True)]
    return {"run_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "summary": summarise(scores), "scores": scores, "results": results}  # fmt: skip


def save(rep: dict) -> str:
    path = ROOT / "eval" / "results" / "e2e_latest.json"
    path.write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str) + "\n",
                    encoding="utf-8")  # fmt: skip
    return str(path.relative_to(ROOT))

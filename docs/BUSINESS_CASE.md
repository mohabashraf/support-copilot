# Business case: is this worth running at ~10,000 tickets/month, and with which configuration?

**Answer: yes. Run it with Claude Haiku for every LLM call (configuration C1), as a co-pilot that drafts and routes, with an agent sending every reply.** It costs about **$710/month**, mostly maintenance; the model itself is about **$7**. It saves an estimated **1.1–2.9 agents' worth of time ($1,390–5,480/month)**. Hard rules held in every measured configuration. Agent-side figures are assumptions that Valetax must confirm in a 2-week shadow run before go-live.

## 1. Which configuration: measured, same 86 tickets, same prompts (2026-11-24)

| | **C1 Haiku / Haiku** | C2 Haiku / Sonnet drafts | C3 Sonnet / Sonnet |
|---|---|---|---|
| Release gate (0 hard-rule violations, all must-route tickets routed) | **PASS** | PASS | PASS |
| Advice declined · must-route routed · uncited numbers | 19/19 · 59/59 · 0 | 19/19 · 59/59 · 0 | 19/19 · 59/59 · 0 |
| Facts correct · right page cited · category | 43/45 · 40/40 · 76/80 | 44/45 · 41/41 · 76/80 | 43/45 · 40/40 · 75/80 |
| Latency p50 / p95 | 3.8 s / 11.6 s | 4.3 s / 10.2 s | 4.5 s / 12.4 s |
| **LLM cost per ticket → per month at 10,000** | **$0.0007 → $7** | $0.0051 → $51 | $0.0140 → $140 |

Decision rule: safety and quality first, then the **cheapest configuration that passes**. All three pass with the same quality, so the answer is **C1**. A larger model buys nothing measurable here, because the hard rules are enforced in code, not by the model. Raw results: `eval/results/e2e_run9.json`, `e2e_C2_haiku_sonnet.json`, `e2e_C3_sonnet_sonnet.json`. Prices: Anthropic list, 2026-11-24.

## 2. Is it worth it at 10,000 tickets/month

| Per month | Conservative | Base |
|---|---|---|
| Agent time saved (drafted replies + pre-triaged routing) | ≈ 173 h ≈ 1.1 FTE | ≈ 457 h ≈ 2.9 FTE |
| **Value** (at $8 / $12 per agent hour, *assumed*) | **≈ $1,390** | **≈ $5,480** |
| Cost: LLM (measured) + hosting + 2 engineer-days maintenance | ≈ $710 | ≈ $710 |
| **Net** | **≈ +$680** | **≈ +$4,770** |

Positive even in the conservative case. The real costs are **maintenance and review time**, not the model. The biggest lever is the **usable-draft rate** (40% → 60% roughly triples the value), and that grows with KB coverage: the abstention log tells Valetax which pages to write. Calculation and assumptions: `BUSINESS_CASE_DETAILS.md`.

## 3. Does it hold up as volume and the KB grow (Phase 2), measured on a clean AWS VM

- **Volume:** 1 worker processed **≈ 960 tickets/hour** (86 in 322 s, all on the first attempt). 10,000/month averages about 14/hour, so that is **~70× headroom**. A 172-ticket burst on 3 workers, with one worker killed mid-run: **172/172 done, 0 lost, 0 duplicated**, and the crashed ticket was re-run automatically.
- **KB growth, speed:** HNSW vector search **p50 1.3 ms at 10,000 chunks and 6.0 ms at 100,000** (p95 9.8 ms); the query embedding (~120 ms) dominates, not the index.
- **KB growth, quality, which is the real risk:** adding 1,000 same-topic internal SOPs dropped Recall@3 **0.90 → 0.69**, because SOPs outrank the customer help pages. **Scoping customer-reply retrieval to customer-facing pages restored it exactly (0.90, Hit@3 0.956, MRR 0.88).** Phase 2 rule: SOPs are agent guidance in their own scope, never cited to customers; the labelled retrieval eval is a release gate on every KB update; add a re-ranker once help pages themselves grow large.

## 4. What must be true, and how to check it before go-live

| Assumption | Check |
|---|---|
| Agent cost/hour, minutes saved per drafted reply | Valetax figures, or a 2-week **shadow run** (co-pilot drafts, agents work as today, compare handling time) |
| Usable-draft rate on *real* tickets (the eval set is deliberately risk-heavy) | Same shadow run: share of drafts sent with no or minor edits |
| 0 hard-rule violations holds on real traffic | Weekly eval + audit-log sample; any violation pauses auto-drafting for that intent |

**Revisit the configuration** if the shadow run shows Haiku drafts need many edits (try C2 on drafting only, at $51/month), volume grows ~5×, or the KB passes ~20–30k tokens.

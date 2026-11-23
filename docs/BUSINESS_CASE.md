# Business case (draft v0.2: LLM numbers still ASSUMPTIONS, infrastructure costs measured)

**Question:** is this worth running at ~10,000 tickets/month, and with which configuration?

> Every number marked *(assumed)* is a placeholder until replaced by (a) measured results from our eval run and (b) Valetax's real figures (agent cost, handling time). LLM cost will be **measured** from real token counts × the provider's price on the day.

## Assumptions

| Assumption | Conservative | Base | Why |
|---|---|---|---|
| Tickets / month | 10,000 | 10,000 | given in the brief |
| Agent cost per hour, loaded *(assumed)* | $8 | $12 | offshore/regional support team incl. overheads |
| Current handling time, repetitive ticket *(assumed)* | 5 min | 6 min | read, search help pages, write, tag, assign |
| Handling time with a good draft *(assumed)* | 3 min | 2 min | read, check citations, edit, send |
| Tickets with a usable draft *(from eval)* | 40% | 60% | rest are hard-rule / out-of-scope / abstained |
| Time saved on abstained/routed tickets *(assumed)* | 0.5 min | 1 min | classification, priority, routing already done |
| Bad drafts that pass the gates *(from eval)* | 5% of drafts | 2% of drafts | cost **+1 min** each (read, reject, rewrite) |

## Calculation

| | Conservative | Base |
|---|---|---|
| Good drafts: tickets × minutes saved | 3,800 × 2 = 7,600 min | 5,880 × 4 = 23,520 min |
| Bad drafts that passed: × −1 min | 200 × −1 = −200 min | 120 × −1 = −120 min |
| Abstained/routed: tickets × minutes saved | 6,000 × 0.5 = 3,000 min | 4,000 × 1 = 4,000 min |
| **Agent time saved / month** | **10,400 min ≈ 173 h** | **27,400 min ≈ 457 h** |
| ≈ full-time agents (160 h/month) | **≈ 1.1 FTE** | **≈ 2.9 FTE** |
| **Value / month** | **≈ $1,390** | **≈ $5,480** |

## Running cost / month *(assumed, to be measured)*

| Item | Small model | Large model |
|---|---|---|
| LLM calls (≈ 3k tokens in, 500 out per ticket; prompt caching reduces input cost) | ≈ $10–50 | ≈ $200–500 |
| Embeddings (tiny KB, one call per ticket) | ≈ $1 | ≈ $1 |
| Hosting (small VM/container + queue in Phase 2) | ≈ $50–100 | ≈ $50–100 |
| Maintenance (≈ 2 engineer-days/month: KB updates, eval, monitoring) | ≈ $600 | ≈ $600 |
| **Total** | **≈ $660–750** | **≈ $850–1,200** |

## Result

| | Conservative + small model | Base + large model |
|---|---|---|
| Value | ≈ $1,390 | ≈ $5,480 |
| Cost | ≈ $750 | ≈ $1,200 |
| **Net / month** | **≈ +$640** | **≈ +$4,280** |

## What this tells us

1. **Worth running at 10,000 tickets/month: yes**, positive even with conservative assumptions. The main value is **absorbing ticket growth without hiring** (~1–3 agents' worth of time), plus faster first replies and consistent, compliant answers.
2. **Model cost is a rounding error.** Even the most expensive model costs less than one minute of agent time on a fraction of tickets. **Maintenance and review effort are the real costs.** So the configuration is chosen on **safety and quality first** (0 hard-rule violations, correct abstention, multilingual quality on the eval set), then the **cheapest model that passes**.
3. **The biggest lever is the usable-draft rate**, i.e. KB coverage. Moving from 40% → 60% roughly triples the value. The abstention log → KB gap report is how we move it.
4. **The biggest risk is bad drafts that pass the gates**: small in minutes, large in compliance exposure. That is why hard-rule violations must be 0 and the `auto_draft_ok` precision is a release-gate metric.

## Recommended configuration (to be confirmed by the eval run)
- Phase 1: top-k retrieval (D1); deterministic pre/post checks; **one model that passes the eval with 0 violations**, choosing the cheaper one if several pass; prompt caching on.
- Revisit when: volume grows ~5×, the KB passes ~20–30k tokens, or measured cost/latency exceeds target.

## Measured so far (2026-11-23)
| Item | Measured | Effect on the case |
|---|---|---|
| PII masking (Presidio + GLiNER, CPU) | ~170 ms per ticket, no API cost | negligible; runs on the same server |
| Retrieval (bge-m3 + pgvector, CPU) | ~45 ms per query, no API cost | negligible |
| End-to-end without LLM calls (`copilot replay`, 76 tickets) | p95 ≈ 250 ms | the 10 s budget is left for the two LLM calls |
| Monitoring (OTel + Grafana LGTM) | open source, self-hosted; Grafana Cloud free tier covers this volume | ≈ $0 Phase 1; Phase 2 depends on Valetax's existing stack |
| **LLM cost and quality per configuration** | **pending**: needs the end-to-end eval run (blocked on a valid API key) | replaces the "Running cost" assumptions above |

Until the LLM run, the conclusion rests on the assumption table; the infrastructure side adds no material cost.


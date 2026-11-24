# Business case (v1.0: LLM and infrastructure costs MEASURED; agent-side figures still assumed)

**Question:** is this worth running at ~10,000 tickets/month, and with which configuration?

> LLM cost, latency and quality are **measured** (end-to-end eval on the shipped code, 86 tickets, `eval/results/e2e_run8.json`). Numbers marked *(assumed)* are agent-side figures that only Valetax can supply (agent cost, handling time, real ticket mix); replace them before deciding.

## Assumptions

| Assumption | Conservative | Base | Why |
|---|---|---|---|
| Tickets / month | 10,000 | 10,000 | given in the brief |
| Agent cost per hour, loaded *(assumed)* | $8 | $12 | offshore/regional support team incl. overheads |
| Current handling time, repetitive ticket *(assumed)* | 5 min | 6 min | read, search help pages, write, tag, assign |
| Handling time with a good draft *(assumed)* | 3 min | 2 min | read, check citations, edit, send |
| Tickets with a usable draft *(assumed; see note)* | 40% | 60% | rest are hard-rule / out-of-scope / abstained. The eval set is deliberately risk-heavy (33% drafted on the labelled set, 18% on the adversarial set), so it can't give the real-traffic rate: measure it in shadow mode |
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

## Running cost / month *(LLM measured; hosting/maintenance estimated)*

| Item | Small model (measured: Claude Haiku) | Large model (headroom) |
|---|---|---|
| LLM calls (measured: **$0.000716 per ticket**) | **≈ $7** | even at 10× the per-ticket cost: < $100 |
| Embeddings (tiny KB, one call per ticket) | ≈ $1 | ≈ $1 |
| Hosting (small VM/container + queue in Phase 2) | ≈ $50–100 | ≈ $50–100 |
| Maintenance (≈ 2 engineer-days/month: KB updates, eval, monitoring) | ≈ $600 | ≈ $600 |
| **Total** | **≈ $660–710** | **≈ $750–800** |

## Result

| | Conservative + small model | Base + large model |
|---|---|---|
| Value | ≈ $1,390 | ≈ $5,480 |
| Cost | ≈ $710 | ≈ $800 |
| **Net / month** | **≈ +$680** | **≈ +$4,680** |

## What this tells us

1. **Worth running at 10,000 tickets/month: yes**, positive even with conservative assumptions. The main value is **absorbing ticket growth without hiring** (~1–3 agents' worth of time), plus faster first replies and consistent, compliant answers.
2. **Model cost is a rounding error.** Even the most expensive model costs less than one minute of agent time on a fraction of tickets. **Maintenance and review effort are the real costs.** So the configuration is chosen on **safety and quality first** (0 hard-rule violations, correct abstention, multilingual quality on the eval set), then the **cheapest model that passes**.
3. **The biggest lever is the usable-draft rate**, i.e. KB coverage. Moving from 40% → 60% roughly triples the value. The abstention log → KB gap report is how we move it.
4. **The biggest risk is bad drafts that pass the gates**: small in minutes, large in compliance exposure. That is why hard-rule violations must be 0 and the `auto_draft_ok` precision is a release-gate metric.

## Recommended configuration (confirmed by the eval run)
- Phase 1: top-k retrieval (D1); deterministic pre/post checks; **Claude Haiku**: passes the release gate with 0 hard-rule violations, so there is no reason to pay for a larger model; prompt caching on.
- Revisit when: volume grows ~5×, the KB passes ~20–30k tokens, or measured cost/latency exceeds target.

## Measured so far (2026-11-23)
| Item | Measured | Effect on the case |
|---|---|---|
| PII masking (Presidio + GLiNER, CPU) | ~170 ms per ticket, no API cost | negligible; runs on the same server |
| Retrieval (bge-m3 + pgvector, CPU) | ~45 ms per query, no API cost | negligible |
| End-to-end without LLM calls (`copilot replay`, 76 tickets) | p95 ≈ 250 ms | the 10 s budget is left for the two LLM calls |
| Monitoring (OTel + Grafana LGTM) | open source, self-hosted; Grafana Cloud free tier covers this volume | ≈ $0 Phase 1; Phase 2 depends on Valetax's existing stack |
| **LLM cost (Claude Haiku, end to end, 86 tickets)** | **$0.000716 per ticket** → ≈ $7 / 10,000 tickets | confirms point 2: model cost is a rounding error |
| **Latency, end to end** | p50 3.5 s; p95 11.6 s (7.6 s in an earlier run) | p95 is **over the 10 s budget**. The slowest tickets are the first batch of the run (E01–E04, 13–20 s: cold start) and tickets needing 4 LLM calls (a redraft after post_check). Fine for a co-pilot (an agent reads the draft later, async queue), but Phase 2 should stream/parallelise or cap redrafts |
| **Safety (release gate)** | 0 hard-rule violations; must-route recall 59/59; trading advice declined 19/19 (in the customer's language 19/19); abstain correct 9/10; complaint / KYC / AML never decided: 15/15 routed to the right team, 0 outcome claims | gate PASS |
| **Quality** | citation page correct 40/40; required facts 44/45; over-routing 3/27; partial answers correct 5/9 | over-routing costs agent minutes, not safety; partial answers are the main thing to improve |

The cost and safety side is now measured. The conclusion still depends on the agent-side assumptions (cost per hour, minutes saved, usable-draft rate), which Valetax's own data or a 2-week shadow run should replace.


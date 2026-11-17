# Architecture (initial, v0.1)

Two phases from the brief: **Phase 1 ships fast**, **Phase 2 scales**. The co-pilot core is identical in both; only the edges change.

## Volume and what it implies

| | Calculation | Result |
|---|---|---|
| Per month | given | 10,000 |
| Per day | 10,000 ÷ 30 | ≈ 330 |
| Per hour (average) | 330 ÷ 24 | ≈ 14 |
| Peak hour | ≈ 5× average (business hours) | ≈ 70 |
| Peak per minute | 70 ÷ 60 | ≈ 1–2 |

Throughput is not the problem; **reliability and safety are**. No Kafka. Phase 1 needs no queue at all.

---

## Phase 1: ship ASAP

The co-pilot core is a **LangGraph workflow (state machine)**, not autonomous multi-agents: two LLM nodes, deterministic nodes around them, conditional edges for routing (decision D3).

```
 tickets.jsonl / CLI ──► Helpdesk adapter (file mock) ──► Ticket model
                                                              │
 ┌────────────────────────── LangGraph: CO-PILOT CORE ────────┼──────────────────────────────┐
 │                                                            ▼                              │
 │                                               ┌────────────────────────┐                  │
 │                                               │ pii_mask        (code) │ typed            │
 │                                               │ + rule flags, ticket   │ placeholders     │
 │                                               │ wrapped as DATA        │ [CARD_1]...      │
 │                                               └───────────┬────────────┘                  │
 │                                                           ▼                               │
 │                                               ┌────────────────────────┐                  │
 │                                               │ classify    (LLM #1)   │ structured JSON: │
 │                                               │ schema-validated       │ category, urgency│
 │                                               │                        │ language, senti- │
 │                                               └───────────┬────────────┘ ment, confidence │
 │                                                           ▼                               │
 │                                               ┌────────────────────────┐                  │
 │                                               │ policy_gate     (code) │                  │
 │                                               └─────┬────────────┬─────┘                  │
 │                             blocked: trading advice,│            │allowed                 │
 │                             complaint, KYC/AML      ▼            ▼                        │
 │                                 ┌────────────────────┐   ┌────────────────────────┐       │
 │                                 │ template_reply     │   │ draft       (LLM #2)   │◄── KB │
 │                                 │ (code): decline /  │   │ grounded, cites chunk  │ whole │
 │                                 │ neutral acknowledge│   │ IDs, or ABSTAINS       │ KB in │
 │                                 └─────────┬──────────┘   └───────────┬────────────┘ prompt│
 │                                           │                          ▼              + emb.│
 │                                           │              ┌────────────────────────┐ scores│
 │                                           │              │ post_check      (code) │       │
 │                                           │              │ citations exist? every │       │
 │                                           │              │ number in cited text?  │       │
 │                                           │              │ no advice language?    │       │
 │                                           │              └──┬──────────────────┬──┘       │
 │                                           │          pass   │   fail (1st) ────┘          │
 │                                           │                 │   → redraft once            │
 │                                           │                 │   fail (2nd) → route        │
 │                                           ▼                 ▼                             │
 │                                 ┌──────────────────────────────────────────┐              │
 │                                 │ route (code): auto_draft_ok |            │              │
 │                                 │ human_required + reason codes;           │              │
 │                                 │ support_agent · senior_support ·         │              │
 │                                 │ compliance                               │              │
 │                                 └────────────────────┬─────────────────────┘              │
 └──────────────────────────────────────────────────────┼────────────────────────────────────┘
                                                        ▼
                    results.jsonl + CLI view + KB explorer web page (FR-9)
                    + audit log (draft AND final, prompt/KB versions, model, sources, cost)
                    + optional Langfuse trace (one span per node)

 Offline:  eval/tickets_labelled.jsonl ──► eval runner ──► docs/BENCHMARK.md
           (release gate: hard-rule violations must be 0)
```

**Graph state** (one object passed between nodes): `ticket`, `masked_text`, `pii_map` (never sent to the LLM), `rule_flags`, `classification`, `gate_decision`, `retrieval_scores`, `draft`, `citations`, `abstained`, `post_check_results`, `attempts`, `route`, `reason_codes`, `usage` (tokens, cost, latency).

---

## Phase 2: scale up

```
 Customer (email / chat / form)
          │
          ▼
 ┌─────────────────┐  1. new ticket → automation rule → WEBHOOK (HTTPS POST + secret)
 │ Helpdesk        │───────────────────────────────────┐
 │ (Freshdesk /    │                                   ▼
 │  Zendesk / ...) │                        ┌─────────────────────┐
 └─────────────────┘                        │ Ingest API (FastAPI)│ 2. verify secret, validate,
          ▲                                 │                     │    dedupe by ticket ID,
          │                                 └──────────┬──────────┘    reply 200 fast
          │                                            ▼ 3. enqueue
          │                                 ┌─────────────────────┐
          │                                 │ Queue (Redis/SQS/   │──► Dead-letter queue
          │                                 │ RabbitMQ)           │    (failed 3× → tag
          │                                 └──────────┬──────────┘     "manual handling")
          │                                            ▼ 4. workers (scale horizontally)
          │                                 ┌─────────────────────┐
          │                                 │ Worker = CO-PILOT   │ same core as Phase 1
          │                                 │ CORE                │◄── Retrieval service
          │                                 └──────────┬──────────┘    (hybrid search + rerank
          │                                            │                over versioned KB/SOPs)
          │  6. REST API: private note (draft +        ▼               ◄── Cache (repeat questions)
          │     citations), tags, priority, group  ┌──────────────┐
          └────────────────────────────────────────│ Helpdesk     │ 5. outbox: retries,
                                                   │ writer       │    respects API rate limits
                                                   └──────────────┘

 Cross-cutting: audit log · metrics & alerts (violations, abstain rate, latency, cost)
                · KB ingestion pipeline (change detection, conflict report to content owners)
                · continuous eval (production samples + agent edits → golden set)
```

**Human in the loop in both phases:** the co-pilot only ever writes a **private note**. An agent reads, edits and sends. Nothing reaches a customer automatically.

## Key design choices (details in DECISIONS.md)

| Choice | Why |
|---|---|
| Helpdesk adapter in front of the core | Don't build a helpdesk; plug into the one they use. Phase 1 = JSONL mock. |
| Deterministic checks before **and** after the LLM | Hard rules can't depend on the model behaving. |
| Structured outputs + schema validation | Predictable, testable, injection-resistant. |
| Abstain as a first-class outcome | KB has real gaps (fees, payment methods, withdrawal limits). |
| Simple queue, not Kafka | 1–2 tickets/min at peak; need retries and spike absorption, not throughput. |
| Fail-open | If the co-pilot is down, agents work as before. |
| Idempotent by ticket ID | Webhooks can be delivered twice. |

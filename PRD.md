# PRD: Support-Ticket Co-pilot

**Status:** Draft v0.1 · **Owner:** Mohab Nazmy · **Date:** 2026-11-17
**Timebox:** 3–5 hours of build · **Deliverables:** Git repo + one-page business case

---

## 1. Problem

The support team of a retail forex/CFD broker spends most of its time answering the same questions about **deposits, withdrawals, account opening and verification**. Agents copy answers from help pages by hand. That is slow, inconsistent, and risky in a regulated business: a wrong fee, a wrong timeline or anything that sounds like trading advice creates compliance exposure.

## 2. Goal

A **co-pilot for support agents** (not an autonomous bot) that, for every incoming ticket:

1. **Classifies** it: category, urgency, language, sentiment.
2. **Drafts a reply** grounded in the knowledge base, **with citations**, or **abstains** when the knowledge base does not cover it.
3. **Routes** to a human reviewer whenever required.

The agent always sends the final reply. The co-pilot saves time and keeps answers consistent and compliant.

### Success metrics (measured on the evaluation set)
| Metric | Target (Phase 1) |
|---|---|
| Hard-rule violations (trading advice, invented policy, deciding KYC/AML/complaints) | **0** |
| Category accuracy | ≥ 90% |
| Correct abstention on out-of-KB questions | ≥ 95% |
| Citation correctness (cited page actually supports the claim) | ≥ 95% |
| Routing recall on must-route tickets (complaint, KYC, AML, trading advice) | **100%** |
| p95 latency per ticket | < 10 s |
| Cost per ticket | measured and reported in the business case |

## 3. Users

- **Support agent (primary):** sees the classification, the draft reply with citations, and the routing decision; edits and sends.
- **Senior reviewer / compliance:** receives routed tickets (complaints, KYC, AML, low-confidence).
- **Support lead (secondary):** reads metrics: volume by category, abstention rate, routing rate, cost.

## 4. Scope

### In scope
- Ticket in → classification + draft (or abstain) + routing decision out.
- Knowledge base = the four public help pages (deposit, withdraw, open account, verify account), **used as they are, including gaps, overlaps and inconsistencies**.
- CLI and/or minimal web page.
- Evaluation set + scoring script.
- One-page business case.

### Out of scope (Phase 1)
- Sending replies to customers automatically.
- Integration with a real helpdesk (mocked; see §9).
- Access to customer account data (balances, transaction status). Mocked/stubbed with an explanation.
- Multi-turn conversations.

## 5. Functional requirements

### FR-1 Ticket intake
- FR-1.1 Accept a ticket with: `id`, `subject`, `body`, optional `channel`, `customer_id`, `created_at`.
- FR-1.2 Accept single tickets (CLI) and batches (JSONL file).
- FR-1.3 Strip/flag PII before any LLM call where possible (e.g. card numbers, document numbers).

### FR-2 Classification
- FR-2.1 **Category** (one of): `deposit`, `withdrawal`, `account_opening`, `verification_kyc`, `complaint`, `trading_advice_request`, `aml_compliance`, `other`.
- FR-2.2 **Urgency**: `low` / `medium` / `high` / `critical`, with defined rules (e.g. missing funds > 24 h, account blocked, legal threat → high/critical).
- FR-2.3 **Language**: ISO code (e.g. `en`, `ar`, `ru`, `es`). Reply drafted in the customer's language.
- FR-2.4 **Sentiment**: `positive` / `neutral` / `negative` / `angry`.
- FR-2.5 Output is **structured (JSON, schema-validated)**; invalid output is retried once, then routed to a human.
- FR-2.6 Each classification has a **confidence**; below threshold → route.

### FR-3 Grounded reply drafting
- FR-3.1 Answer **only** from the knowledge base. Every factual claim cites the page(s) used (URL + section/FAQ title).
- FR-3.2 **Abstain** when the KB does not cover the question (e.g. fees, payment methods, withdrawal limits): draft a holding reply that says the information is being checked, and route.
- FR-3.3 When KB pages **conflict** (e.g. "instant" vs "within 60 minutes"), use the more conservative/specific statement and flag the conflict to the agent.
- FR-3.4 Do **not** use known-defective content (the withdraw page's steps are copy-pasted from deposit).
- FR-3.5 Deduplicate the FAQ that appears on both the deposit and withdraw pages, so citations stay clean.
- FR-3.6 Tone: polite, short, professional; matches the customer's language.

### FR-4 Hard rules (guardrails)
- FR-4.1 **Never give trading advice**: no buy/sell recommendations, signals, price targets or leverage suggestions. Detect these requests and **decline politely**. Stating a documented fact (e.g. "maximum leverage is up to 1:2000 depending on account type") is allowed; recommending a setting is not.
- FR-4.2 **Never decide a complaint, KYC or AML outcome.** Draft a neutral acknowledgement and route to a person.
- FR-4.3 **Never invent policy, fees or timelines.** Cite the KB or say it is unknown.
- FR-4.4 Guardrails are enforced in **two layers**: deterministic checks (rules/regex/allowlists) **and** LLM classification; a **post-generation check** scans the draft for advice language, unsupported numbers (any number/fee/timeline not present in the cited KB text) and missing citations.
- FR-4.5 Treat ticket text as **untrusted input** (prompt-injection defence): instructions inside a ticket are never followed.

### FR-5 Routing
- FR-5.1 Route to a human when **any** of: category is complaint / KYC / AML; trading-advice request; abstention; low confidence; post-check failure; angry sentiment with high urgency; invalid model output.
- FR-5.2 Output a routing decision: `auto_draft_ok` (agent reviews and sends) or `human_required` with **reason codes**.
- FR-5.3 Route destination: `support_agent`, `senior_support`, `compliance`.

### FR-6 Output
- FR-6.1 One JSON record per ticket: classification, draft, citations, abstained flag, routing decision + reasons, KB conflicts flagged, latency, tokens, cost.
- FR-6.2 Human-readable view in CLI / web page.

### FR-7 Evaluation
- FR-7.1 Labelled evaluation set (~25–40 tickets) covering: each category, multiple languages, out-of-KB questions, trading-advice traps, KYC/AML/complaints, KB inconsistencies, prompt-injection attempts.
- FR-7.2 Scoring script reporting the metrics in §2.
- FR-7.3 The evaluation run acts as a **release gate**: hard-rule violations must be 0.

### FR-8 Business case
- FR-8.1 One page answering: **is this worth running at ~10,000 tickets/month, and with which configuration?**
- FR-8.2 Compare configurations (e.g. small vs large model, with/without caching) on cost per ticket, quality and latency, using measured numbers from the evaluation run plus stated assumptions (agent cost, handling time saved).

## 6. Non-functional requirements

| ID | Area | Requirement |
|---|---|---|
| NFR-1 | Safety & compliance | Zero tolerance for hard-rule violations; every draft reviewed by an agent before sending. |
| NFR-2 | Auditability | Log every ticket: input hash, prompt version, KB version, model, retrieved sources, output, routing reasons, timestamp. |
| NFR-3 | Privacy | Minimise PII sent to the LLM; no customer data stored beyond logs; secrets via environment variables, never in the repo. |
| NFR-4 | Latency | p95 < 10 s processing per ticket; draft ready within ~1 min of arrival (agent-assist, not real-time chat). |
| NFR-4b | Throughput | Peak ≈ 70 tickets/hour (≈ 1–2/min, from 10,000/month × 5× peak factor); absorb spikes up to 10× (e.g. payment-provider outage). |
| NFR-4c | Availability | **Fail-open:** if the co-pilot is down, agents keep working manually in the helpdesk; the co-pilot never blocks support. |
| NFR-4d | Idempotency | The same ticket ID processed twice produces one draft (webhooks can be delivered twice). Ordering not required. |
| NFR-5 | Cost | Cost per ticket tracked; target well below agent handling cost (quantified in the business case). |
| NFR-6 | Scalability | Phase 2 handles ~10,000 tickets/month and growth (peaks, larger KB) without redesign. |
| NFR-7 | Reliability | LLM timeouts/errors → retry with backoff, then route to human; the system never silently drops a ticket. |
| NFR-8 | Maintainability | Prompts, KB and rules versioned in the repo; provider-agnostic LLM interface; config-driven thresholds. |
| NFR-9 | Testability | Deterministic parts unit-tested; evaluation set runnable with one command. |
| NFR-10 | Observability | Metrics: volume by category, abstention rate, routing rate, violations, latency, tokens, cost. |
| NFR-11 | Multilingual | Classify and reply in the customer's language; KB is English-only, so translated answers still cite English sources. |
| NFR-12 | Portability | Python; runs locally with one setup command; any LLM provider. |

## 7. Phases

### Phase 1: Ship ASAP
- Simplest design that meets the hard rules: small KB loaded directly (decision open, see §10), one structured classification call, one grounded drafting call, deterministic guardrails before and after the LLM.
- CLI + optional minimal web page.
- Evaluation set + scoring script.

### Phase 2: Scale up (~10,000 tickets/month, growing KB and SOPs)
- Retrieval over a larger KB (hybrid search + reranking, metadata, versioning).
- Async queue/worker processing, retries, dead-letter queue, idempotency.
- Caching of repeated questions; cheaper model for classification, stronger model only where needed.
- Helpdesk integration (e.g. Zendesk/Freshdesk webhook), monitoring dashboards, alerting on violations.
- KB ingestion pipeline with change detection and conflict reporting to content owners.
- Continuous evaluation: production samples added to the golden set; agent edits used as a quality signal.

## 8. Assumptions
- Tickets arrive as text (email/chat/form); attachments ignored in Phase 1.
- English KB; customers may write in other languages.
- Agents review every draft in Phase 1.
- ~10,000 tickets/month today; growth expected. ≈ 330/day, ≈ 14/hour average, ≈ 70/hour at peak.
- Tickets arrive from an existing helpdesk (e.g. Freshdesk/Zendesk) via webhook; replies go back as a **private note** via its REST API. Phase 1 mocks this with JSONL. We do **not** build a helpdesk (see docs/ARCHITECTURE.md).
- Phase 1 processes tickets directly (no queue). Phase 2 adds a simple queue (Redis/RabbitMQ/SQS) for retries and spikes; Kafka is not justified at this volume.

## 9. Mocks and stubs (explained in README)
- **Helpdesk integration:** mocked by JSONL in/out files. Real version: webhook from the helpdesk, reply posted back as an internal draft.
- **Customer account lookup** (e.g. withdrawal status): stubbed. Real version: read-only API to the back office, scoped per ticket.
- **Human review queue:** mocked as an output field/file. Real version: helpdesk view or assignment.

## 10. Open decisions (to be made and justified by the owner)
1. How the KB is given to the model in Phase 1: full KB in prompt vs keyword search vs embeddings.
2. LLM provider and model(s): one model or a small/large split.
3. Classification and drafting in one call or two.
4. Confidence: model self-report vs rules vs both.
5. UI: CLI only, or CLI + minimal web page.

## 11. Risks
| Risk | Mitigation |
|---|---|
| Model gives trading advice | Deterministic detector + LLM category + post-generation check + decline template |
| Invented fees/timelines | Abstain rule + post-check for numbers not found in cited KB text |
| KB contradictions | Conflict flags, conservative answer, report to content owners |
| Prompt injection in tickets | Ticket text wrapped as data; output schema + allowlisted routes; no tools with side effects |
| Cost growth at volume | Small model by default, caching, measured cost per ticket |

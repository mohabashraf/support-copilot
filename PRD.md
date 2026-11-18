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
- FR-1.3 **Replace PII with typed placeholders** before any LLM call: e.g. `4111 1111 1111 1111` → `[CARD_1]`, passport/ID numbers → `[DOC_1]`, emails → `[EMAIL_1]`, phone numbers → `[PHONE_1]`, IBAN/account numbers → `[ACCOUNT_1]`.
  - The placeholder → original mapping is kept **only in our service** (per ticket, in memory / encrypted store), never sent to the LLM or written to logs in clear text.
  - The model still understands the ticket ("customer shared a card number `[CARD_1]`") without seeing the value.
  - Drafts may contain placeholders; they are re-filled only where needed, and **sensitive values (full card or document numbers) are never echoed back** to the customer; at most masked (e.g. `**** 1111`).
  - Detection: deterministic patterns (regex + Luhn check for cards) in Phase 1; a PII-detection library/model in Phase 2.

### FR-2 Classification
- FR-2.1 **Category = the topic** (one of): `deposit`, `withdrawal`, `account_opening`, `verification`, `complaint`, `trading_advice_request`, `aml_compliance`, `other`. Jurisdiction/eligibility questions ("I live in X, can I open an account?") are `aml_compliance`.
- FR-2.1b **Decision requested** (separate from the topic): `decision_requested`: `none` | `complaint` | `kyc` | `aml`. Set when the customer asks us to decide an outcome ("approve my verification", "refund me now", "unfreeze my account"). A process question about verification ("how long does it take?") has `decision_requested = none` and is answerable. Maps directly to the brief's rule: never decide a complaint, KYC or AML outcome.
- FR-2.1c **No-question tickets** (e.g. a thank-you): short polite draft, no citation required, not counted as a citation failure.
- FR-2.1d **Multi-intent tickets:** classification returns a **list of intents**, each with its own topic, `decision_requested`, and the text span it refers to (e.g. "1) documents needed? 2) I want to complain about agent Sam"). The ticket's overall category = the most restrictive intent. Gating, drafting and routing work **per intent** (FR-4.6).
- FR-2.1e **Safety and security signals** (classification flags, also detected by deterministic rules):
  - `vulnerable_customer`: distress, hopelessness, self-harm or financial-hardship language ("I lost everything", "no reason to go on").
  - `security_sensitive`: requests about another person's account or data, changing contact details (email, phone), disabling 2FA, password or login-security changes.
- FR-2.2 **Urgency**: `low` / `medium` / `high` / `critical`, with defined rules (e.g. missing funds > 24 h, account blocked, legal threat → high/critical).
- FR-2.3 **Language**: ISO code of the ticket (e.g. `en`, `ar`, `ru`, `es`), plus **`reply_language`**: the language the reply must be written in. An explicit request ("please reply in Arabic") wins over the detected ticket language; otherwise `reply_language = language`. Arabic replies follow the MSA rule (§8).
- FR-2.4 **Sentiment**: `positive` / `neutral` / `negative` / `angry`.
- FR-2.5 Output is **structured (JSON, schema-validated)**; invalid output is retried once, then routed to a human.
- FR-2.6 Each classification has a **confidence**; below threshold → route.

### FR-3 Grounded reply drafting
- FR-3.1 Answer **only** from the knowledge base. Every factual claim cites the **page used**: page title + **page URL** (e.g. `How to deposit — https://valetax.com/how-to-deposit/`) + section/FAQ title. Internal chunk IDs (e.g. `deposit#min-deposit`) are kept alongside for the post-check and the audit log, but the citation shown to the agent always includes the page URL.
- FR-3.2 **Abstain** when the KB does not cover the question (e.g. fees, payment methods, withdrawal limits): draft a holding reply that says the information is being checked, and route. The holding reply **must not promise any timeline** (e.g. "within 24 hours"), because that would be an invented timeline (FR-4.3). Approved wording: "A member of our team is reviewing your request and will reply as soon as possible."
- FR-3.2b **Partial coverage:** answer the covered part with citations and flag the uncovered part to the agent; never guess the missing part.
- FR-3.3 When KB pages **conflict** (e.g. "instant" vs "within 60 minutes"), use the more conservative/specific statement and flag the conflict to the agent.
- FR-3.3b **KB used as it is:** the four pages are ingested **unchanged** (no rewriting or "fixing" of the text). Gaps, overlaps and inconsistencies are **detected and flagged**, and the co-pilot's *handling* of them is a documented choice (FR-3.3–3.5), never an edit of the source.
- FR-3.4 **Flag** known-defective content and do not rely on it for answers (the withdraw page's steps are copy-pasted from deposit: "amount to deposit", "Start Trading!"). The chunk stays in the KB, marked `defective`, visible in the explorer.
- FR-3.5 The FAQ that appears on both the deposit and withdraw pages is stored once with **both page URLs** as sources, so citations stay clean and remain traceable to each page.
- FR-3.6 Tone: polite, short, professional; matches the customer's language.

### FR-4 Hard rules (guardrails)
- FR-4.1 **Never give trading advice**: no buy/sell recommendations, signals, price targets or leverage suggestions. Detect these requests and **decline politely**. Stating a documented fact (e.g. "maximum leverage is up to 1:2000 depending on account type") is allowed; recommending a setting is not.
- FR-4.2 **Never decide a complaint, KYC or AML outcome.** Triggered by `decision_requested != none` (FR-2.1b) or topic `complaint` / `aml_compliance`. Draft a neutral acknowledgement and route to a person.
- FR-4.3 **Never invent policy, fees or timelines.** Cite the KB or say it is unknown.
- FR-4.4 Guardrails are enforced in **two layers**: deterministic checks (rules/regex/allowlists) **and** LLM classification; a **post-generation check** scans the draft for advice language, unsupported numbers (any number/fee/timeline not present in the cited KB text) and missing citations. Before comparing, **all digits are normalised** to ASCII 0–9 (Arabic-Indic ٠–٩, Persian ۰–۹, Thai ๐–๙ and other Unicode decimal digits), so "٨ دولار" matches "8 USD" in the KB.
- FR-4.5 Treat ticket text as **untrusted input** (prompt-injection defence): instructions inside a ticket are never followed, including instructions in quoted email threads, in other languages, or written as JSON that imitates our own output schema.
- FR-4.6 **Per-intent gating:** each intent is gated separately. Allowed intents get a grounded draft; blocked intents get their template (decline / neutral acknowledgement); the reply combines both in the intent order. **If any intent is blocked, the whole ticket is routed** to the strictest destination (compliance > senior_support > support_agent).
- FR-4.7 **Vulnerable customers:** when `vulnerable_customer` fires, the ticket is routed immediately at **critical** urgency to senior support. The co-pilot provides **no template-only reply and no product talk** (no deposits, trading, offers); it drafts only a short, caring acknowledgement for a human to adapt, and the agent is shown an internal note that this is a vulnerable-customer case. (Wording and any support-resource guidance are owned by Valetax's compliance/support policy, not invented by the co-pilot.)
- FR-4.8 **Security-sensitive requests:** never reveal, confirm or guess any account data, and never confirm a change of contact details or security settings. Draft a neutral reply stating the request will be handled through the secure verification process, and route to senior support.

### FR-5 Routing
- FR-5.1 Route to a human when **any** trigger fires, each emitting a reason code:

  | Code | Trigger | Detection |
  |---|---|---|
  | `hard_rule_topic` | trading advice, complaint, KYC/AML | classification + deterministic keyword rules |
  | `not_in_kb` | model abstained | draft step returns `abstained: true` |
  | `weak_retrieval` | nothing relevant found | retrieval score below threshold / no source matched |
  | `low_confidence` | classification confidence below threshold (default 0.7, configurable) | model confidence |
  | `post_check_failed` | claim without citation, number/fee/timeline not in cited text, advice language | deterministic post-checks |
  | `kb_conflict` | KB pages disagree on the point asked | conflict flags from ingestion |
  | `angry_urgent` | angry sentiment + high/critical urgency | rules on classification |
  | `technical_failure` | invalid JSON after one retry, LLM timeout/error | schema validation, error handling |
  | `low_resource_language` | ticket language where model quality is weak (initially ckb, sw; configurable) or translation confidence is low | language detection + config list + confidence |
  | `account_action_required` | KB covers the guidance but resolving the ticket needs the customer's account checked or changed (e.g. approved withdrawal not received, deposit failed) | classification flag `needs_account_action` + rules |
  | `vulnerable_customer` | distress, hopelessness, self-harm or hardship signals | classification flag + deterministic phrase rules (both; either fires) |
  | `security_sensitive` | another person's account/data, contact-detail change, 2FA/password/security changes | classification flag + deterministic rules |
  | `multi_intent_blocked` | at least one intent of a multi-intent ticket is blocked by the policy gate | per-intent gate results |

  For `low_resource_language`, the agent receives the classification and an **English draft** with citations instead of a possibly poor translation of a financial answer.

  Model confidence is never trusted alone (models are overconfident); deterministic checks back it up.
- FR-5.2 Output a routing decision: `auto_draft_ok` (agent reviews and sends) or `human_required` with **reason codes**.
- FR-5.3 Route destination: `support_agent`, `senior_support`, `compliance`.

### FR-5b Human-in-the-loop for account actions (LangGraph interrupt)
- FR-5b.1 When `account_action_required` fires, the workflow **pauses** (LangGraph `interrupt`, state saved by a checkpointer) and asks the agent for the missing account facts (e.g. "withdrawal W-123 sent on 17 Nov, ref ABC").
- FR-5b.2 Phase 1: the back-office lookup is a **stub** that returns "not available", so the agent always supplies the facts. Phase 2: a read-only back-office API is tried first; the interrupt happens only if it can't answer.
- FR-5b.3 When the agent submits the facts, the workflow **resumes** from the saved state and drafts the reply grounded in the KB **plus the agent-provided facts**, which are cited as `agent-provided` (not as KB) and logged in the audit record with the agent's ID.
- FR-5b.4 If no facts arrive (agent skips), the ticket stays `human_required` with the holding reply; nothing is invented.

### FR-6 Output
- FR-6.1 One JSON record per ticket: classification, draft, citations (**page title + page URL + section**, plus internal chunk ID), abstained flag, routing decision + reasons, KB conflicts flagged, latency, tokens, cost.
- FR-6.2 Human-readable view in CLI / web page.

### FR-7 Evaluation
- FR-7.1 Labelled evaluation set (~25–40 tickets) covering: each category, multiple languages, out-of-KB questions, trading-advice traps, KYC/AML/complaints, KB inconsistencies, prompt-injection attempts.
- FR-7.2 Scoring script reporting the metrics in §2.
- FR-7.3 The evaluation run acts as a **release gate**: hard-rule violations must be 0.

### FR-9 Knowledge-base explorer (web view)
- FR-9.1 A web page listing every KB chunk: source page, section/FAQ title, text, chunk ID, and flags (duplicate, conflict, known-defective, rule-sensitive).
- FR-9.2 **Search playground:** type a question → see the top-k retrieved chunks with similarity scores, and whether the abstain threshold would trigger.
- FR-9.3 Per-ticket trace: for a processed ticket, show which chunks were retrieved and which were cited in the draft.
- FR-9.4 (Optional) 2-D map of chunk embeddings (PCA/UMAP) to show clusters by topic and spot overlaps.
- Purpose: debugging retrieval, demoing the system, and letting support leads see exactly what the co-pilot "knows".

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
| NFR-11 | Multilingual | Classify and reply in the customer's language; KB is English-only, so translated answers still cite English sources. The **number/fee/timeline post-check runs after translation**. Quality in languages the team can't read is checked by back-translation + LLM judge in eval, and by native agents' edit rate per language in production. |
| NFR-12 | Portability | Python; runs locally with one setup command; any LLM provider. |

## 6b. Compliance, audit and observability

### Three separate layers
| Layer | Purpose | Readers | Phase 1 | Phase 2 |
|---|---|---|---|---|
| **Audit log** | Legal record of what the system did and why, per ticket | Compliance, auditors | Append-only JSONL, always on | Insert-only Postgres table or write-once storage (e.g. S3 Object Lock); retention per compliance |
| **LLM tracing** | Debugging and quality: prompts, model calls, tokens, latency, cost | Engineers | **Langfuse**, optional via config flag (synthetic data only) | Langfuse **self-hosted**, instrumented through **OpenTelemetry** so the vendor is swappable |
| **App logs and metrics** | Operations: errors, throughput, alerts | DevOps / on-call | Structured JSON logs | Prometheus/Grafana or the cloud provider's tools; alerts on violations, abstain rate, latency, cost |

The tracing tool is **not** the audit log: it is for debugging, may have limited retention, and holds ticket text, so it is self-hosted or PII-masked.

### Audit-log record (one per ticket)
- Ticket ID, timestamps, channel; **PII-masked** text (placeholders) + hash of the original.
- Model name/version, **prompt version**, **KB version** (hash), retrieved/cited chunk IDs + scores.
- Classification, guardrail results, routing decision + reason codes.
- **Both the AI draft and the final text sent**, plus which agent reviewed/edited/sent it and when.
  - Why both: an auditor can see what the AI suggested vs what the human decided ("who told the customer this, based on what?"), and the draft-vs-final diff is the **edit rate** quality metric.

### Compliance requirements and open questions for Valetax
- **Data flow** (documented in README): ticket → our service (PII placeholders) → LLM provider (masked text only) → our audit store; traces to self-hosted Langfuse.
- **LLM provider terms:** no training on our data, zero data retention where available, Data Processing Agreement, known region. Verified against each provider's current terms, not assumed.
- **Retention period:** set by Valetax compliance per their licence (Mauritius FSC) and customer jurisdictions. **Not invented here.**
- **Privacy vs retention:** keep masked audit records for the required period; store the PII mapping separately with tighter access and shorter life.
- **Access control:** engineers see masked traces; compliance sees the audit log; nobody sees raw PII by default.

## 7. Phases

Both phases are **built**, not only designed. Phase 1 is tagged in git (`v1-ship`) so reviewers can see the "ship ASAP" version on its own; Phase 2 builds on it.

### Phase 1: Ship ASAP (git tag `v1-ship`)
- Top-k retrieval with multilingual embeddings (D1, revised); search returns vector and text together (D9); LangGraph workflow with two LLM calls and deterministic gates (D3).
- CLI + web app (ticket view, KB explorer, search playground) (D5).
- Evaluation set + benchmark (docs/BENCHMARK.md).

### Phase 2: Scale up (~10,000 tickets/month, growing KB and SOPs) (git tag `v2-scale`)
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
- **Languages:** the help site exists in 13 languages (en, ar, ckb/Kurdish Sorani, id, ms, th, vi, zh-TW, ru, sw, fr, pt, es), so tickets are assumed multilingual. The real ticket split by language is unknown (ask Valetax). Evaluation is tiered:
  - Tier 1 (deep): English, Arabic (incl. dialects and Arabizi).
  - Tier 2 (light, different scripts): Russian, Thai, Indonesian, Vietnamese.
  - Tier 3 (fallback test, lower-resource): Kurdish (Sorani), Swahili.
  - Smoke test if time allows: Chinese, Malay, French, Spanish, Portuguese.
- **Arabic register:** customers may write in Egyptian/Gulf/Iraqi dialect or Arabizi; replies are always in **clear, simple Modern Standard Arabic** (warm, not stiff), in Arabic script. Reasons: precision and consistency for regulated financial wording, one register for compliance review, MSA is understood by all Arabic speakers, and LLM dialect generation is less reliable and can sound unprofessional. If a customer writes only in Arabizi/Latin script and may not read Arabic script, the agent is offered an English version too. Revisit with data (CSAT/edit rate by dialect).
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
5. ~~UI~~ decided: see D5.

## 11. Risks
| Risk | Mitigation |
|---|---|
| Model gives trading advice | Deterministic detector + LLM category + post-generation check + decline template |
| Invented fees/timelines | Abstain rule + post-check for numbers not found in cited KB text |
| KB contradictions | Conflict flags, conservative answer, report to content owners |
| Prompt injection in tickets | Ticket text wrapped as data; output schema + allowlisted routes; no tools with side effects |
| Cost growth at volume | Small model by default, caching, measured cost per ticket |

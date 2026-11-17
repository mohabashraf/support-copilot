# Decision log

One entry per design decision. This file is shared with reviewers; it shows *why* the system is built this way.
Template: Context → Options → Decision → Why → Trade-offs / revisit when.

## D1. How the knowledge base reaches the model (Phase 1)
- **Status:** decided (2026-11-17)
- **Context:** KB = 4 pages, ~1,800 words (~2,500 tokens after cleaning and deduping the shared FAQ); ~10,000 tickets/month; citations required; abstention must be measurable; a KB explorer web view is required (FR-9).
- **Options:** A) embeddings + vector DB · B) keyword search (BM25) · C) whole KB in the prompt
- **Decision:** **C + embeddings.**
  - The **whole cleaned KB goes into the prompt**, split into labelled chunks (`[deposit#min-deposit]`, ...), so the model can cite chunk IDs.
  - The same chunks are **also embedded** and stored in a small local vector index. Each ticket is scored against them.
- **Why:**
  - **Ship fast and maximise recall:** with the whole KB in context, no relevant fact can be "missed by retrieval". At this size it is cheap (~2.5k input tokens per ticket).
  - **Measurable abstain signal:** the top similarity score gives a deterministic `weak_retrieval` trigger (e.g. best score < threshold → abstain/route), independent of the model's self-reported confidence.
  - **Citations check:** chunk IDs let the post-check confirm every cited chunk exists and contains the numbers used in the draft.
  - **Explorer (FR-9):** embeddings power the search playground and the optional 2-D map, so support leads can see what the co-pilot knows.
  - **Phase 2 ready:** when the KB and SOPs grow, the prompt switches from "whole KB" to "top-k retrieved chunks" with no change to the rest of the pipeline.
- **Trade-offs / revisit when:**
  - Sending the whole KB costs more tokens than top-k; acceptable now, measured in the business case. Prompt caching reduces it further.
  - Embedding similarity on a tiny KB is a coarse signal; the threshold is tuned on the eval set, not guessed.
  - **Switch to top-k retrieval (+ hybrid BM25 + reranker) when** the KB exceeds ~20–30k tokens, or cost/latency per ticket rises above target.
- **Options rejected for Phase 1:**
  - A alone (vector DB + top-k only): adds retrieval misses with no benefit at this size.
  - B (BM25): weak on paraphrases and non-English tickets.

## D2. LLM provider and model(s)
- **Status:** decided as a *method*; final model filled from docs/BENCHMARK.md
- **Context:** any provider allowed; Python preferred; model cost is small vs agent time (business case), so quality and safety decide.
- **Decision:**
  - **Provider:** Anthropic (Claude) for Phase 1, behind a small provider-agnostic LLM interface; structured outputs for classification.
  - **Model:** chosen by **benchmark**, not opinion. Candidates: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, plus a split (Haiku classifies, Sonnet drafts). Model IDs are config, not code.
  - **Rule:** 0 hard-rule violations and passing all gates → cheapest configuration wins.
  - **Embeddings:** a **local multilingual, cross-lingual** model (e.g. bge-m3 or multilingual-e5), because tickets arrive in many languages while the KB is English, and no ticket text leaves for embeddings. Anthropic has no embeddings API; an English-only model (e.g. all-MiniLM) would fail cross-lingual matching.
- **Why:** makes the trade-off visible to the business with numbers (docs/BENCHMARK.md).
- **Trade-offs / revisit when:** a small eval set can't prove a rate of 0 (rule of three); revisit with production data, new model releases, or price changes.

## D3. Classification and drafting: one call or two (and orchestration)
- **Status:** decided (2026-11-18)
- **Context:** hard rules (trading advice, complaint/KYC/AML outcomes) must be enforced before any reply is written; routed tickets should cost as little as possible; the benchmark compares a split configuration (C2).
- **Options:** one LLM call (classify + draft together) · two LLM calls with a deterministic gate between · autonomous multi-agent system
- **Decision:**
  - **Two LLM calls:** `classify` (structured output) → deterministic `policy_gate` → `draft` (grounded, cites, or abstains) only if allowed.
  - **Orchestrated with LangGraph as a workflow (state machine)**, not as autonomous multi-agents. Nodes: `pii_mask` → `classify` → `policy_gate` → `draft` | `template_reply` → `post_check` → (`redraft` once) → `route`.
- **Why:**
  - **Safety order:** with one call, the model would already have written the forbidden content before we check it. The gate sits between classification and drafting.
  - **Cost:** blocked/routed tickets skip the drafting call.
  - **Model split:** enables a cheaper classification model (benchmark C2).
  - **Testability:** classification and drafting are evaluated separately, so failures are attributable.
  - **Why LangGraph:** the graph *is* the routing logic (readable, drawable), bounded retry loops are simple, `interrupt` + checkpointer support human review in Phase 2, Langfuse traces it via callback, and I've run it in production.
  - **Why not multi-agent:** the steps are fixed and the hard rules must be enforced by code, not by an agent's judgement. Multiple autonomous agents would add cost, latency and failure points without benefit here.
- **Trade-offs / revisit when:** two calls add some latency (acceptable vs the 10 s target); LangGraph adds a dependency vs plain Python. Revisit splitting into agents only if one step grows too large or needs its own tools (e.g. a back-office lookup agent in Phase 2).

## D4. Confidence: model self-report, rules, LLM judge, or a combination
- **Status:** decided (2026-11-18)
- **Context:** the co-pilot must know when it is not sure and route to a human (8 triggers, PRD FR-5.1). Models are often overconfident; deterministic checks are cheap and explainable but can't judge meaning.
- **Options:** model self-reported confidence · deterministic rules · LLM-as-a-judge · combination
- **Decision: route if ANY signal fails** (layers, cheapest first):
  1. **Deterministic rules (always):** embedding best-score threshold (`weak_retrieval`), citations exist, every number/fee/timeline appears in the cited chunk text, no advice language, schema valid.
  2. **Model self-report (always):** `confidence` in the classification output, below threshold → `low_confidence`. Never trusted alone.
  3. **LLM-as-a-judge:**
     - **Offline (always):** in the eval run, a judge scores faithfulness (is every claim supported by the cited chunks?), tone, and language quality, as part of docs/BENCHMARK.md.
     - **Runtime (optional node `groundedness_judge`, after `post_check`):** checks each claim of the draft against the cited chunks; unsupported → `post_check_failed`. **Kept only if the benchmark shows it catches errors the rules miss** (e.g. "deposits are free", which contains no number).
     - Judge uses a **different model from the drafter** to reduce correlated errors.
- **Why:** rules catch the measurable failures for free; the judge catches *meaning* errors that rules can't; self-report adds a cheap early signal. No single signal is trusted alone.
- **Trade-offs / revisit when:** a runtime judge adds one more LLM call (latency + small cost, negligible vs agent time per the business case) and is itself an LLM that can be wrong. Thresholds are tuned on the eval set, not guessed. Benchmark compares with/without the runtime judge.

## D5. UI: CLI only, or CLI + minimal web page
- **Status:** decided (2026-11-18)
- **Context:** brief says "a minimal CLI or simple web page is enough"; FR-9 already requires a KB explorer web view; reviewers get a 60-minute walkthrough; 3–5 h timebox.
- **Decision: CLI + one small web app.**
  - **CLI** (`copilot run tickets.jsonl`, `copilot eval`): batch processing and the eval/benchmark run; what reviewers can run in one command.
  - **Web app** (FastAPI + server-rendered HTML, **no frontend framework**), three pages:
    1. **Ticket view:** submit or pick a ticket → classification, draft with clickable citations, abstained flag, routing decision + reason codes, tokens/cost/latency, node-by-node trace.
    2. **KB explorer (FR-9):** all chunks with flags (duplicate, conflict, defective, rule-sensitive).
    3. **Search playground (FR-9):** question → top-k chunks with similarity scores and whether the abstain threshold triggers.
- **Why:** the CLI satisfies the brief and the eval; the web page makes the walkthrough visual (show a trading-advice ticket being blocked, an out-of-KB ticket abstaining, citations opening the source chunk) and serves the explorer requirement with the same backend.
- **Trade-offs / revisit when:** web page costs ~1 h of the timebox; kept deliberately plain (no React, no auth, local only). In Phase 2 the real UI is the helpdesk itself (private note), so this page becomes an internal admin/debug tool.

## D6. Observability and audit
- **Status:** decided (2026-11-18)
- **Context:** regulated broker; tickets contain PII; every decision must be explainable to an auditor; 3–5 h timebox; reviewers must be able to run the repo without extra services.
- **Options:** LangSmith · Langfuse · Arize Phoenix · Helicone · audit log only
- **Decision:**
  - **Our own append-only audit log, always on** (JSONL in Phase 1), storing **both the AI draft and the final sent text**.
  - **Langfuse for LLM tracing**, optional via config flag (`TRACING=langfuse`); the app runs fully without it.
  - **Phase 2:** Langfuse self-hosted + **OpenTelemetry** instrumentation, so the tracing vendor is swappable.
- **Why:**
  - Audit ≠ tracing: the audit log is the legal record and must be ours, complete and retained per regulation.
  - Langfuse is open source and self-hostable for free → in production, ticket data stays on the broker's infrastructure. Works without LangChain.
  - Chosen over LangSmith (which I've run in production) because LangSmith self-hosting needs an enterprise plan; over Helicone because a proxy adds a third party in the data path; Phoenix is a fine alternative but one tool is enough.
- **Trade-offs / revisit when:** Langfuse self-hosting needs several services (Postgres, ClickHouse, Redis, object storage), so it is not set up in Phase 1; demo uses the free cloud tier with synthetic tickets only. Revisit if Valetax already standardises on another tool (OpenTelemetry makes the switch cheap).

## D7. Vector store: pgvector (with an in-memory index for tests)
- **Status:** decided (2026-11-18)
- **Context:** retrieval provides the abstain signal (`weak_retrieval`), the KB explorer and search playground, and all retrieval in Phase 2 (growing KB + SOPs). Phase 2 already needs a relational database for the audit log, ticket status and idempotency keys. Today the KB is 26 chunks; with SOPs it may reach thousands, not millions. Reviewers should be able to run the core without Docker.
- **Options:**
  - In-memory only (numpy): simplest, but no persistence and no Phase 2 path.
  - **pgvector** (PostgreSQL extension): vectors in the same database as everything else.
  - Qdrant: dedicated vector database (used in an earlier project of mine).
  - Chroma: embedded vector store, mainly for prototypes.
- **Decision:** **pgvector** as the real vector store, behind a `VectorIndex` interface with two implementations:
  - `PgVectorIndex`: Postgres + pgvector (Docker Compose), used by the app, the eval runs and Phase 2.
  - `InMemoryIndex`: numpy, used in unit tests and the no-Docker quick start.
  - Selected by config: `VECTOR_STORE=pgvector | memory`.
- **Why pgvector:**
  1. **One database, not two.** Phase 2 needs Postgres anyway (audit log, tickets, idempotency). pgvector stores vectors in the same database, so there is one system to run, secure, back up and monitor. Qdrant would add a second database for no real gain at this scale.
  2. **Right size for the scale.** HNSW indexing handles thousands to millions of vectors well. Qdrant's advantages (advanced vector features, very large scale, heavy vector traffic) don't apply to a KB of tens to thousands of chunks.
  3. **SQL where it helps.** Filtering by flags (e.g. exclude `defective`, `promo`) is a normal `WHERE`; audit records can be joined with the chunks they cited; transactions keep KB versions consistent.
  4. **Hybrid search in one query (Phase 2).** Postgres full-text search provides the keyword half next to the vector half, merged with RRF, without another service.
  5. **Regulated-broker fit.** One place for access control, encryption at rest and backups, using standard Postgres tooling that operations teams already know.
  6. **Experience.** I've used PostgreSQL with pgvector for retrieval in production, so I can operate and defend it.
- **Why the interface + in-memory implementation:** unit tests run with no database; reviewers can run the core without Docker; the rest of the system depends only on `VectorIndex`, so the store can change without touching the workflow.
- **Trade-offs / revisit when:**
  - Requires Docker for the full app (documented in the quick start; memory mode remains available).
  - Postgres full-text search is weaker for some languages than dedicated BM25 engines; measured in the Phase 2 hybrid benchmark.
  - **Revisit (e.g. Qdrant or a managed vector service) when** vectors reach many millions, vector queries dominate database load, or advanced vector features (multi-vector, quantisation at scale) are needed.

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

## D4. Confidence: model self-report, rules, or both
- **Status:** open

## D5. UI: CLI only, or CLI + minimal web page
- **Status:** open

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

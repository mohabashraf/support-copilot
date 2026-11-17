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
- **Status:** open

## D3. Classification and drafting: one call or two
- **Status:** open

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

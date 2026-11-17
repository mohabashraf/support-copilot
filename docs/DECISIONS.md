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

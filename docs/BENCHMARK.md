# Model benchmark (template v0.1: numbers filled by the eval run)

**Purpose:** give the business real numbers so the model choice is a visible trade-off (safety, quality, speed, cost), not a guess.
**Decision rule (from the business case):** safety and quality first → then the **cheapest configuration that passes**.

## Setup
- **Eval set:** `eval/tickets_labelled.jsonl`, N = 42 tickets (see eval/README.md), labelled with expected category, urgency, language, sentiment, must-route flag, expected abstention, and the KB facts a correct answer must contain.
- **Coverage:** every category · Tier 1–3 languages + Arabizi · out-of-KB questions · trading-advice traps · complaint/KYC/AML · KB inconsistencies · prompt-injection attempts · partial-coverage tickets.
- **Fixed for all runs:** same prompts (version ___), same KB (hash ___), same embeddings model (___), same thresholds.
- **Prices:** provider list price on run date ___ (link), cache pricing applied where used.
- **Runs:** each configuration run ___ times (LLM output varies); report mean and worst run.

## Configurations
| ID | Classification model | Drafting model | Effort | Prompt caching |
|---|---|---|---|---|
| C0 (baseline) | as C2, but the **whole KB** in the draft prompt, no retrieval (D1 superseded design) | | ___ | on |
| C1 | `claude-haiku-5-5` | `claude-haiku-5-5` | ___ | on |
| C2 | `claude-haiku-5-5` | `claude-sonnet-5-5` | ___ | on |
| C3 | `claude-sonnet-5-5` | `claude-sonnet-5-5` | ___ | on |
| C4 | `claude-opus-5-5` | `claude-opus-5-5` | ___ | on |
| C2+J | `claude-haiku-5-5` | `claude-sonnet-5-5` + runtime `groundedness_judge` (different model, e.g. `claude-haiku-5-5` or `claude-opus-5-5`) | ___ | on |

All configurations except C0 draft from the **top-k retrieved chunks** (D1). C0 only measures what retrieval costs in answer quality.

Judge decision: keep the runtime judge only if C2+J catches unsafe/unsupported drafts that C2's deterministic post-checks miss, at acceptable latency.

## Results
| Metric | Gate | C1 | C2 | C3 | C4 |
|---|---|---|---|---|---|
| **Hard-rule violations** (count / N) | **= 0** | | | | |
| Must-route recall (complaint, KYC, AML, trading advice) | = 100% | | | | |
| Correct abstention (out-of-KB) | ≥ 95% | | | | |
| Citation correctness | ≥ 95% | | | | |
| Unsupported numbers in drafts (post-check catches) | report | | | | |
| Category accuracy | ≥ 90% | | | | |
| Language detection accuracy | ≥ 95% | | | | |
| Arabic draft quality (my review, 1–5) | ≥ 4 | | | | |
| **Adversarial pass rate** (34 tickets, `eval/tickets_adversarial.jsonl`) | report | | | | |
| Adversarial hard-rule violations | **= 0** | | | | |
| Over-blocking rate (X20–X22) | report | | | | |
| Known gaps closed (of 8) | report | | | | |
| Usable-draft rate (feeds business case) | report | | | | |
| p50 / p95 latency per ticket | p95 < 10 s | | | | |
| Tokens per ticket (in / out / cached) | report | | | | |
| **Cost per ticket** | report | | | | |
| **Cost per month @ 10,000 tickets** | report | | | | |

## Retrieval (RAG) results
Data: `eval/retrieval_queries.jsonl` (56 answerable + 14 no-answer) and `relevant_chunks` on eval tickets. Definitions: `eval/README.md` Level 2c.

| Retriever | Recall@1 | Recall@3 | Recall@5 | Precision@3 | MRR | Hit@3 (Arabic) | Hit@3 (other non-en) | Abstain recall @ t | False-abstain @ t | AUROC | t chosen |
|---|---|---|---|---|---|---|---|---|---|---|---|
| all-MiniLM-L6-v2 (English-only baseline) | 0.488 | 0.64 | 0.699 | 0.289 | 0.623 | 0.333 | 0.2 | 0.06 | 0.02 | 0.606 | 0.0895 |
| multilingual-e5-small | 0.657 | 0.89 | 0.912 | 0.417 | 0.825 | 0.917 | 1.0 | 0.06 | 0.02 | 0.734 | 0.7362 |
| bge-m3 | | | | | | | | | | | |
| Phase 2: hybrid (vector + BM25, RRF) | | | | | | | | | | | |
| Phase 2: hybrid + reranker | | | | | | | | | | | |

Run 2026-11-18, KB hash `46229716470b7620`, in-memory index (pgvector returns identical rankings: `tests/test_retrieval.py::test_pgvector_matches_memory`). Full reports: `eval/results/retrieval_*.json`. Ticket-text Recall@3: MiniLM 0.601 → e5-small 0.798.

**Findings**
1. **Multilingual embeddings are required (confirms D2).** Hit@3 in Arabic rises from 0.33 to 0.92 and in other languages from 0.20 to 1.00; MRR from 0.62 to 0.83. The English-only model sends almost every Arabic query to `open#faq-islamic`.
2. **The embedding score alone is a weak abstain signal (confirms D4).** With e5-small every best score falls in 0.72–0.85; no-answer queries (fees, PayPal, spreads) score 0.73–0.85, overlapping answerable ones. At ≤ 2% false abstain it catches only 6% of no-answer queries (AUROC 0.73). So `weak_retrieval` is a minor signal; abstention must come mainly from the drafting LLM ("not in KB") plus deterministic checks. Phase 2 test: does a cross-encoder reranker separate the scores better?
3. **Remaining e5-small misses** (12 of 68 with Recall@3 < 1) are mostly one side of a conflict pair or a secondary required chunk (R07, R09, R26, R34), plus R02 ("start trading with 5 dollars" → funds-available), R29 (jurisdiction → notice) and R33 (Arabic euros). Candidates for bge-m3 and the reranker.

## How to read small-sample results (important for the business)
- **0 violations in 40 tickets does not prove 0%.** Rule of three: with 0 failures in N tests, the true rate could still be up to about **3/N** (95% confidence) → up to **~7.5%** for N = 40. More tickets narrow this: N = 300 → ≤ 1%.
- **1 violation in 40 = 2.5%** observed → at 10,000 tickets/month that is on the order of **~250 unsafe drafts per month** for an agent to catch. Not acceptable as a default, even if 20× cheaper.
- Therefore: the benchmark picks a **candidate**; Phase 2 keeps measuring in production (violation alerts, agent edit rate) before any reduction of human review.

## Trade-off summary for the business (fill after the run)
| Configuration | Safe? | Quality | Speed | Cost / month | Recommendation |
|---|---|---|---|---|---|
| C1 | | | | | |
| C2 | | | | | |
| C3 | | | | | |
| C4 | | | | | |

**Recommendation:** ___ because ___. Revisit when ___.

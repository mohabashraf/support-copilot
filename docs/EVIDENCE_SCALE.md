# Evidence: clean install, retrieval and answers through Docker, and Phase 2 scale

Measured 2026-11-24 on a fresh AWS EC2 `m7i.xlarge` (4 vCPU, 16 GB, Ubuntu 24.04), from a clean `git clone` of tag `v1-ship`, exactly as a reviewer runs it. Scripts: `eval/scale/`.

## 1. Clean install: `cp .env.example .env && docker compose up --build`
- **Empty `.env`:** build, model download and KB indexing done in **3 min 34 s**. The indexer read the 4 help pages → 26 chunks → bge-m3 vectors in pgvector (`kb_chunks` 26 rows). The app then stops with one message naming the missing secrets and how to generate them with Docker (`services.require_secrets`).
- **No Hugging Face token:** the gated injection guard (Llama Prompt Guard 2) is not accessible, so the app loads the measured no-token fallback (`NeuralTrust/prompt-guard-oss-small`) and logs a warning (`services.load_guard`).
- **Restart:** the indexer embeds nothing (`unchanged=26, embedded=0`).

## 2. Retrieval on the Docker-built index (`copilot eval retrieval --store pgvector`)
| | Recall@3 | Hit@3 | MRR | n |
|---|---|---|---|---|
| Queries | **0.90** | **0.956** | **0.88** | 68 answerable (+17 no-answer) |
| Eval tickets | 0.845 | 0.893 | 0.863 | 28 |

Same as the development index (`BENCHMARK.md`).

## 3. Answers through the running API (`eval/scale/api_eval.py`: 86 tickets → POST /tickets → worker → GET)
Release gate **PASS**: 0 hard-rule violations, must-route 59/59, advice declined 19/19 (in the customer's language 19/19), complaint/KYC/AML to the right team 15/15, 0 uncited numbers, required facts 44/45, citations 40/41, language 80/80, $0.00072/ticket, p50 3.5 s / p95 9.0 s.
*Note:* the API does not return the internal draft object, so this script cannot score abstention; `copilot eval e2e` does (10/10 in run 9).

## 4. Volume (Phase 2)
| Test | Result |
|---|---|
| 1 worker, 86 tickets | 322 s → **≈ 960 tickets/hour**; all done on the first attempt; processing p50 3.4 s, p95 8.8 s |
| 3 workers, 172-ticket burst, **one worker killed mid-run** | **172/172 done, 0 failed, 0 duplicates**; the crashed ticket was re-run after its lease expired (attempt 2). Sending was the bottleneck (133 s), so **≥ 1,800 tickets/hour** |
| 10,000 tickets/month | ≈ 14/hour average → one worker has **~70× headroom** |

Finding: recovery waits for the lease (~5 min; the crashed ticket took 318 s end to end). Phase 2: shorter lease + heartbeat.

## 5. KB growth (Phase 2)
**Speed** (HNSW, random 1,024-d vectors; `eval/scale/kb_scoped_and_speed.py`):
| Vectors | HNSW build | Search p50 | Search p95 |
|---|---|---|---|
| 1,026 (real + SOPs) | n/a | 5.5 ms | 5.9 ms |
| 10,000 | 7.8 s | 1.3 ms | 1.6 ms |
| 100,000 | 139 s | 6.0 ms | 9.8 ms |

Query embedding (~120 ms on CPU) costs more than the search.

**Quality** (26 real chunks + 1,000 synthetic SOPs on the same topics, with fees, limits and timelines; `eval/scale/kb_growth.py`):
| Retrieval for customer replies | Recall@3 | Hit@3 | MRR |
|---|---|---|---|
| Help pages only (today) | 0.90 | 0.956 | 0.88 |
| + 1,000 SOPs, **unscoped** | **0.689** | 0.735 | 0.686 |
| + 1,000 SOPs, **scoped to customer-facing pages** | **0.90** | **0.956** | **0.88** |

SOPs on the same topics outrank help pages (18 of 25 failures had an SOP in the top 3). **Phase 2 rule:** SOPs live in their own scope as agent guidance and are never cited to customers. The retrieval eval is a release gate on every KB update. Add a re-ranker when the help pages themselves grow. For very large scopes, filtered HNSW needs pgvector's iterative scan or a separate index.

**Not measured:** 5,000 and 10,000 *real-text* SOP chunks (CPU embedding ≈ 13 min per 1,000 chunks on this VM; a GPU or an embedding API is the Phase 2 path for bulk ingestion).

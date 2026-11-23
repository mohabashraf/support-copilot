# Decision log

One entry per design decision. This file is shared with reviewers; it shows *why* the system is built this way.
Template: Context → Options → Decision → Why → Trade-offs / revisit when.

## D1. How the knowledge base reaches the model
- **Status:** **revised 2026-11-18** (first decided 2026-11-17; the original is kept below as superseded)
- **Context:** KB = 4 pages, 26 chunks (~2,500 tokens) today; the brief says the KB **and SOPs will grow** and Phase 2 targets ~10,000 tickets/month; citations required; abstention must be measurable.
- **Decision:** **top-k retrieval only.** The drafter sees the k best chunks for the ticket (labelled with chunk ID, page title and URL), never the whole KB. One code path for Phase 1 and Phase 2.
- **Why (the owner challenged the first version):**
  - **The KB grows:** whole-KB-in-prompt stops working as soon as the SOPs arrive; designing for it means rebuilding in Phase 2.
  - **Tighter citations:** the drafter can only cite chunks it was given, so the post-check verifies a small set instead of any of N chunks; similar FAQs are mixed up less.
  - **The retrieval eval becomes the real quality gate:** R@k, MRR and abstain AUROC (`copilot eval retrieval`) decide what the LLM sees, so they measure something that matters.
  - **Cost and latency stay flat** as the KB grows.
  - **Simpler serving (D9):** nothing needs the whole KB in memory, so the server keeps no KB snapshot.
- **What it costs, and the mitigations:** retrieval misses (R@3 = 0.90, R@5 = 0.93 with bge-m3; 0.89 / 0.91 with multilingual-e5-small) → k chosen from the eval; chunks flagged `conflict` pull in the chunk they conflict with; Phase 2 adds hybrid BM25 + reranker; weak retrieval routes to a human instead of guessing (`weak_retrieval`, `not_in_kb`).
- **Measured, not assumed:** whole-KB stays as benchmark baseline **C0** (docs/BENCHMARK.md), to report how much answer quality top-k loses, if any.
- **Superseded first version (2026-11-17):** "whole cleaned KB in the prompt + embeddings for the abstain signal": maximised recall at this size (no retrieval misses) and was cheap with prompt caching, but did not survive the KB growing, and kept the whole KB in memory while serving.

## D2. LLM provider and model(s)
- **Status:** decided as a *method*; final model filled from docs/BENCHMARK.md
- **Context:** any provider allowed; Python preferred; model cost is small vs agent time (business case), so quality and safety decide.
- **Decision:**
  - **Provider:** Anthropic (Claude) for Phase 1, behind a small provider-agnostic LLM interface; structured outputs for classification.
  - **Model:** chosen by **benchmark**, not opinion. Candidates: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, plus a split (Haiku classifies, Sonnet drafts). Model IDs are config, not code.
  - **Rule:** 0 hard-rule violations and passing all gates → cheapest configuration wins.
  - **Embeddings:** a **local multilingual, cross-lingual** model, because tickets arrive in many languages while the KB is English, and no ticket text leaves for embeddings. Anthropic has no embeddings API; an English-only model (all-MiniLM) fails cross-lingual matching (Arabic Hit@3 0.33).
  - **Embedding model: `bge-m3` (decided 2026-11-22, from docs/BENCHMARK.md finding 4).** Same Recall@3 as multilingual-e5-small (0.90 vs 0.89) but better ranking (Recall@1 0.75 vs 0.66, MRR 0.88 vs 0.83), Arabic Hit@3 1.00 vs 0.92 and a better abstain signal (AUROC 0.79 vs 0.73). Costs accepted: +33 ms per query (small next to the LLM call), ~2.2 GB RAM, slower indexing. Judgement call for a multilingual-first product; e5-small is the fallback (`EMBEDDING_MODEL` + one `kb index`). Retrieval thresholds are per model.
- **Why:** makes the trade-off visible to the business with numbers (docs/BENCHMARK.md).
- **Trade-offs / revisit when:** a small eval set can't prove a rate of 0 (rule of three); revisit with production data, new model releases, or price changes.

## D3. Classification and drafting: one call or two (and orchestration)
- **Status:** decided (2026-11-18)
- **Context:** hard rules (trading advice, complaint/KYC/AML outcomes) must be enforced before any reply is written; routed tickets should cost as little as possible; the benchmark compares a split configuration (C2).
- **Options:** one LLM call (classify + draft together) · two LLM calls with a deterministic gate between · autonomous multi-agent system
- **Decision:**
  - **Two LLM calls:** `classify` (structured output) → deterministic `policy_gate` → `draft` (grounded, cites, or abstains) only if allowed.
  - **Orchestrated with LangGraph as a workflow (state machine)**, not as autonomous multi-agents. Nodes: `classify` → `policy_gate` → `retrieve` → `draft` | `template_reply` → `post_check` → (`redraft` once) → `compose` → `route` → `audit`.
  - **Revised 2026-11-23: PII masking runs BEFORE the graph** (`process_ticket`), not as its first node: anything that enters a LangGraph run is recorded in the state, the LangSmith trace and Studio, so raw PII must never enter it. The placeholder → value mapping goes to the encrypted vault (D10).
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
- **Status:** **revised 2026-11-23** (first decided 2026-11-18; kept below as superseded)
- **Revised decision: the API exposes only what the business uses.**
  - `POST /tickets`: the helpdesk sends a ticket and gets a ticket number (202); `GET /tickets/{number}`: status, progress and result (D11). `GET /health`: Docker's healthcheck. Responses carry no ticket text and no PII values.
  - **Agent endpoints** (added 2026-11-23, JSON, no HTML): `GET /tickets` (inbox), `GET /tickets/{number}`, `GET /tickets/{number}/customer-details` (audited), `POST /tickets/{number}/decision` (audited). Agents act through a frontend or the helpdesk; Auth: API keys with roles (helpdesk | agent), hashes only in `.env`, fail closed; the agent's identity comes from the key. API docs only with `API_DOCS=true`. Phase 2: SSO (OIDC) for agents, signed webhooks / mTLS for the helpdesk.
  - KB explorer, search playground and PII preview are **not** HTTP endpoints. Developers use the CLI (`copilot kb show`, `copilot search`), LangSmith for per-ticket traces, LangGraph Studio for the graph.
  - **Why (owner's call):** every endpoint is attack surface and maintenance. A PII-preview endpoint in particular would let anyone probe what the masker misses. The real UI is the helpdesk itself (Phase 2: reply posted as a private note).
- **Superseded first version (2026-11-18):**
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
- **Status:** **revised 2026-11-23** (first decided 2026-11-18: Langfuse; kept below as superseded)
- **Context:** regulated broker; tickets contain PII; every decision must be explainable to an auditor; the owner wants to see the system **end to end** (API → masking → workflow → LLM → routing → database), not only LLM calls.
- **Decision: four layers, each with one job.**

  | Layer | Job | Tool (Phase 1) | Phase 2 |
  |---|---|---|---|
  | **Audit log** | legal record per ticket: processing (masked-text hash, steps, routing, AI draft, cited chunks), PII reveals (who, which types), agent decisions (AI draft vs final text, re-masked) | ours: append-only JSONL (`audit.py`) | insert-only Postgres table / WORM storage |
  | **System monitoring** | rates, latency, errors, PII residual rate, routing mix, cost; alerts | **OpenTelemetry → Grafana LGTM** (Tempo traces, Prometheus metrics, Loki logs), local `grafana/otel-lgtm` container | Grafana Cloud or self-hosted LGTM, or Valetax's Datadog: same OTel code, new endpoint |
  | **LLM debugging** | prompts, tokens and cost per call, why a draft is what it is | **LangSmith** (agent `support-copilot`, env `local`), masked text only | LangSmith self-hosted, or Langfuse self-hosted (both OTel-capable) |
  | **App logs** | operations, one JSON line per event, never ticket text | JSON file + `copilot logs` / `copilot pii-report`, exported to Loki | the company's log platform |

- **Why:**
  - **OpenTelemetry is the vendor-neutral standard:** instrument once (FastAPI, psycopg, our spans and metrics), send anywhere. No lock-in, and a PUSH model, so the API exposes no extra endpoint (D5).
  - **LangSmith is the right tool for LangGraph/LLM debugging** but not for counters, trends and alerts on non-LLM data such as masking. Monitoring belongs in a metrics system.
  - **Self-hostable end to end:** for a broker, ticket data and telemetry can stay on its own infrastructure.
  - **Audit ≠ tracing:** tracing tools have retention limits and are for engineers; the audit log is the legal record and must be ours.
  - **Ticket list in Grafana without PII:** Grafana reads Postgres through the role `grafana_ro`, which may SELECT only the view `tickets_monitoring` (derived non-text columns: status, steps, routing, agent decision, timings); `tickets` (masked message, draft, final text) and `pii_vault` are denied (verified).
  - **Honest telemetry (2026-11-23):** monitoring never reports work that didn't happen. Nodes not implemented yet are marked `@placeholder`; they produce nothing (no invented routing), every step records `implemented`, tickets carry `complete`, `steps_completed` (real work only) and a status per step (`completed` | `placeholder`), and Grafana shows placeholders in amber. Earlier records that held a placeholder's routing were cleared in Postgres; the append-only audit log got an `audit_correction` entry instead of an edit.
  - **Cross-linked IDs:** the OTel `ticket` span carries `langsmith.trace_id`; the LangSmith run carries `otel_trace_id`; logs carry both, so you can go dashboard → trace → logs → LLM call.
- **Superseded first version (2026-11-18):** Langfuse for LLM tracing, OTel in Phase 2. Changed because the owner chose LangSmith (production experience, zero-code LangGraph tracing) and asked for system-wide monitoring now; Langfuse remains the Phase 2 option if LangSmith self-hosting is not available.

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

## D8. Ingestion and serving are separate; ingestion is incremental
- **Status:** decided (2026-11-18)
- **Context:** first version: the server parsed the raw HTML at startup and, if the KB hash had changed, re-embedded every chunk and replaced the vector table. Fine for 4 pages; the brief says the KB and SOPs will grow (Phase 2: ~10k tickets/month).
- **Problems with the first version at scale:**
  1. **Slow, risky startup:** a KB change made the server re-embed everything before answering anything.
  2. **Races:** with several server workers, each one sees "hash changed" and rebuilds the same table.
  3. **All or nothing:** one new page re-embedded the whole KB (delete all, insert all).
  4. **Wrong owner:** a web server needed write access to the knowledge base.
- **Decision:**
  - **One writer:** `copilot kb index` (`retrieval/indexer.py: index_kb`) is the only code that writes the KB. It runs as a job (by hand today; CI or a scheduler on KB/SOP change in Phase 2).
  - **Incremental:** every chunk has a `content_hash` (text, sources, flags). The job embeds only new or changed chunks and deletes removed ones. Vectors are keyed by chunk ID in one table per embedding model.
  - **Postgres holds the chunks too** (`kb_chunks`, whole chunk as JSON, validated by the `Chunk` model on read), so the server never reads raw HTML.
  - **Read-only serving:** the server and the CLI search load the KB from Postgres and check that the model's vectors match it exactly. If not, they stop with "run `copilot kb index`"; they never rebuild silently.
  - **Order:** vectors first, chunks last, so the server only sees a new KB once its vectors exist. A crash in between makes the check fail loudly; re-running the job fixes it.
- **Verified:** first run embeds 26; a second run embeds 0; one edited FAQ re-embeds 1; a removed chunk is deleted with 0 embedded (tests in memory and pgvector). Retrieval eval unchanged (R@3 0.89, MRR 0.825).
- **Trade-offs / revisit when:**
  - A flag-only change re-embeds that chunk (the hash covers flags). Cheap now; split the hash if flags change often.
  - The job takes no lock: run one at a time. Phase 2: a Postgres advisory lock, or the job queue guarantees it.
  - Schema changes are handled by dropping and rebuilding the vector tables (they are derived data). Add migrations (e.g. Alembic) when the audit-log tables arrive, because those are not derived.
  - `VECTOR_STORE=memory` keeps the old build-everything-in-memory path for tests and the no-Docker demo.
- **Follow-ups done (2026-11-18):**
  - **One connection pool per process** (`copilot/db.py`, `psycopg_pool`, size `DB_POOL_SIZE`), shared by `KBStore` and `PgVectorIndex`. Each call borrows a connection and returns it, so concurrent requests don't queue behind one connection and don't pay for a new connection each time.
  - ~~**Hot reload without restart**~~ (superseded by D9 the same day: with no in-memory KB there is nothing to reload) (`KBWatcher`, every `KB_RELOAD_SECONDS`, 0 = off): a cheap query compares the stored chunk hashes with the served KB. If they differ and the vectors are ready, a complete new `Services` is swapped in with **one assignment**, so a request never mixes an old KB with a new retriever. If the vectors aren't ready (indexer mid-run, other model), the old KB keeps serving and it retries next tick. Verified live: a flag change was indexed (1 chunk re-embedded) and served ~2 s later, with no restart.
  - Not done: a `POST /admin/reload` endpoint (needs authentication first); with several server processes each one reloads on its own within one interval.

## D9. Search reads vectors and chunk text together; no in-memory KB while serving
- **Status:** decided (2026-11-18)
- **Context:** after D8 the server kept the chunks in memory (refreshed by a watcher) but read the vectors live from Postgres. The owner spotted the gap: between `copilot kb index` writing vectors and the server refreshing its copy, search could return a chunk ID the server didn't know (→ HTTP 500) or rank by a new vector while citing old text. With D1 revised to top-k, the in-memory copy had no remaining purpose.
- **Decision:**
  - **Versioned vectors:** key `(chunk_id, content_hash)`. A changed chunk gets a new row next to the old one.
  - **Search = one JOIN** of the vector table with `kb_chunks` **on the exact version**; each hit returns its chunk (text, section, page URLs, flags) from the same query (like a vector DB "payload").
  - **Indexer order:** (1) add vectors for new versions (not searchable yet), (2) write chunks in one transaction (new versions searchable at once), (3) delete versions nothing matches.
  - **No KB snapshot in the server:** the explorer and `/health` read `kb_chunks` per request; the hot-reload watcher (built earlier the same day) was removed. A `kb index` run is visible to every server process immediately.
  - Startup still checks that the served model has vectors for every stored chunk (`require_indexed`), to catch "serving a model nobody indexed".
- **Verified:** regression test runs a search between steps 1 and 2: every hit equals the stored version, text included, and a new chunk is not visible until step 2; a running app shows a new chunk on `/health`, in the store and in search results with no reload (test updated 2026-11-23 when the KB/search endpoints were removed, D5). Retrieval eval unchanged (R@3 0.89, MRR 0.825).
- **Trade-offs / revisit when:**
  - Each explorer or health request reads Postgres (fine at this scale; add caching only if measured).
  - HNSW + JOIN + flag filter can return fewer than k rows when many rows are filtered out; at our scale it doesn't, and pgvector's iterative index scans (`hnsw.iterative_scan`) are the fix if it does.
  - The vector table may hold two versions of a chunk during a run (tiny: changed chunks only).

## D10. PII masking: Presidio + GLiNER, with an encrypted vault
- **Status:** decided (2026-11-23)
- **Context:** tickets carry cards, IBANs, phones, passport and account numbers, names and addresses, in many languages (Arabic, Arabizi, Russian, Vietnamese...). PII must not reach the LLM, LangSmith, logs or the audit log. The KB answers need the rest of the text intact (amounts, timelines, countries for compliance routing).
- **Options:** own regex · Presidio with spaCy (English NER) · Presidio + a multilingual NER model (GLiNER-PII) · a cloud PII API
- **Decision:**
  - **Presidio Analyzer** as the framework. **Deterministic, checksum-validated patterns for identifiers** (cards with Luhn, IBAN with checksum, email, phones validated by Google's phonenumbers with Egypt/Gulf regions) + **our recognizers** for document and account numbers that only count next to a context word (English and Arabic).
  - **GLiNER-PII (`urchade/gliner_multi_pii-v1`) as a Presidio recognizer for names and street addresses** in any language, with rules on top: pronouns/relatives/roles are never names; below score 0.8 a name needs a capitalised word that is not just sentence-initial; an address must contain a number (a city or country alone is not PII and the compliance check needs it).
  - **Masking happens before the graph** (D3). Placeholders are typed and numbered in reading order, shared across subject and body (`[NAME_1]` is the same person everywhere).
  - **Encrypted vault:** the placeholder → value mapping is stored in Postgres `pii_vault`, Fernet-encrypted (key in `.env`, KMS in Phase 2), expiring after `PII_RETENTION_DAYS` (30), idempotent per ticket. Cards, documents and accounts are never shown back in full (`**** 1111`).
  - **Monitoring:** per ticket, detections (type, recognizer, score; never values) in the LangSmith `pii_mask` step and as OTel metrics; a **residual check** flags text that still looks like PII after masking (e.g. a mistyped card that fails Luhn) as a WARNING + metric, alertable.
- **Why:** patterns are exact and explainable for identifiers; only a model finds names, and only a multilingual one finds Arabic names. Scores alone could not separate non-names ("friend" 0.87) from names ("Mohamed Samir" 0.56), so lexical rules were added and measured.
- **Measured (`copilot eval pii`, docs/BENCHMARK.md):** on 20 NEW multilingual cases: GLiNER masks **22/22 values** and keeps 24/25 non-PII words; spaCy-only masks 14/22 (misses every non-English name). On the 76 labelled tickets: exact type match 1.00 (optimistic: rules were tuned there). Latency p50 ~170 ms per ticket (spaCy mode 5 ms).
- **Trade-offs / revisit when:** GLiNER adds ~1 GB of model and ~170 ms; 8-digit numbers are masked as phones (valid in Qatar/Bahrain/Oman), e.g. an order number; lowercase Latin names below 0.8 are missed. Revisit with a larger labelled PII set from production samples; add more languages' pronoun lists as they appear.

## D11. Tickets are background jobs: Postgres queue + worker processes
- **Status:** decided (2026-11-23)
- **Context:** processing a ticket means two LLM calls (seconds, sometimes timeouts or rate limits); the helpdesk must not wait or time out; a crash must never lose a ticket (NFR-7); webhooks are delivered more than once (NFR-4d); volume ~10,000/month (≈1–2/min at peak, 10× spikes).
- **Options:** synchronous request · FastAPI BackgroundTasks (in-process) · Celery/RQ + Redis · a Postgres table as the queue
- **Decision: Postgres table `tickets` as the queue, `copilot worker` processes.**
  - `POST /tickets`: mask PII + vault + INSERT (status `queued`) → **202 + ticket number**. The helpdesk ticket ID is UNIQUE: the same ID again returns the existing ticket (200).
  - Worker: claims the oldest claimable ticket with `UPDATE … WHERE number = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`, runs the LangGraph workflow with `stream`, saving each finished node (`current_step`, `steps`), then `done` + result.
  - Errors → back to `queued` with the error, retried up to **3 attempts**, then `failed` **and handed to a person**: routed `human_required`, reason `technical_failure` (plus anything the finished steps found, e.g. an injection → senior support), evidence = the error type; the agent can take it over with `POST /decision` (manual). A `ticket_failed` audit event records it. A claim takes a **5-minute lease**, renewed on every node; a crashed worker's ticket becomes claimable again when the lease expires.
  - **A retry resumes, it does not start over** (added 2026-11-23): the graph runs with LangGraph's **PostgresSaver** checkpointer, one thread per ticket (`ticket-<number>-<helpdesk id>`). State is saved after every step, so attempt 2 continues from the step that failed: finished steps, and LLM calls already paid for, are not run again, and a draft can't silently change between attempts. Each step records its attempt; the result says `resumed_from`. The checkpoint is deleted when the ticket is done and kept when it fails (debugging). It holds masked text only, and `grafana_ro` can't read it. The same checkpointer will carry the account-action pause (FR-5b, LangGraph `interrupt`).
  - **Dedup is by helpdesk ticket ID only** (Phase 2 gap, PRD §7): the same message under two IDs is two tickets, and a follow-up under the same ID is ignored today.
  - `GET /tickets/{number}`: status, current step, steps done, attempts, timings, trace ID, result or error.
- **Why:**
  - **No new infrastructure:** Postgres already holds the KB, vectors and vault; it is durable and transactional. SKIP LOCKED is the standard multi-worker queue pattern; at this volume it is far from its limits.
  - **Only masked text is queued:** masking happens at intake, so the queue holds no raw PII.
  - **Not BackgroundTasks:** in-process jobs die with the API process and can't scale separately.
  - **Not Celery + Redis:** a second datastore to run and secure, and a broker that can lose messages unless configured carefully; nothing here needs its features yet.
- **Observability:** queue depth by status (gauge), queue wait (histogram), failures (retried / final) on the Grafana dashboard; each ticket has an intake trace and a processing trace (LangSmith + OTel), linked by the ticket number.
- **Trade-offs / revisit when:** workers poll every 0.5 s when idle (Phase 2: LISTEN/NOTIFY); a ticket re-runs from the start after a crash or error (nodes are idempotent; Phase 2: LangGraph checkpointer to resume mid-graph, also needed for the human-in-the-loop interrupt, FR-5b); move to a dedicated broker (SQS, RabbitMQ) only if volume grows by orders of magnitude or other services need the events.

## D12. Hard-rule signals: exact patterns first, then a classifier on bge-m3, then two LLM calls
- **Status:** decided (2026-11-23)
- **Context:** hard-rule risks (trading advice, complaint/KYC/AML decisions, vulnerable customer, account security, prompt injection) must be caught in every language the broker serves (13 site languages), independently of the LLM; keyword rules in English + Arabic would leave 11 languages uncovered.
- **Options:** keyword regex per language · translate then English rules · similarity to examples (bge-m3) · **a trained classifier on bge-m3** · guard models (Prompt Guard, Llama Guard) · LLM only
- **Decision:**
  - **`pattern_check` first** (before any model or LLM): a few exact phrases (instruction override, schema spoofing, "disable 2FA", "change my registered email") and **spam/empty** detection. Evidence = the pattern's name. Spam/empty skips every model and LLM call and goes to a person at low priority; an injection hit is passed to the injection gate (see below): no LLM call, no AI draft.
  - **`risk_classifier`**: one logistic-regression head per risk (+ a category head) on frozen **bge-m3** embeddings (already loaded for KB search), trained on `classifier/training_examples.jsonl` (141 English + Arabic examples incl. hard negatives). Per sentence + whole ticket; weak ≥ 0.5 (confirmed with the LLM safety call), strong ≥ 0.8 (blocks alone). Evidence = probability + nearest TRAINING example.
  - **Two LLM calls in parallel** with it: `classify_safety` (tuned for recall) and `classify_triage` (category, urgency, sentiment, language…). `policy_gate` combines all of them in code: the strictest answer wins.
- **Why:** deterministic, local, free (~50 ms), independent of the LLM, multilingual through bge-m3, and it gives a probability instead of a yes/no keyword match. Patterns run first because exact attack strings are certain and must be known before an LLM reads the ticket.
- **Measured** (`copilot eval risks`): on 62 held-out cases in 12 languages (never trained on), patterns + classifier: **recall 42/42 for every risk and language**, precision 0.84, **no false alarm at the strong level**; category accuracy 0.89 without an LLM. Dev (76 labelled tickets): 28/30; the 2 misses (KYC part of an injection ticket, a chargeback at p=0.494) are left to the LLM safety call.
- **Injection is its own gate, not a risk, and it runs BEFORE any LLM** (revised 2026-11-23): injection is an attack on the co-pilot, not a customer topic. `injection_check` runs right after `pattern_check` (layers: exact patterns, the injection head on bge-m3, Prompt Guard 2 next). ANY layer, even a weak model signal → `risk_classifier` (local, so the agent sees what else is in the ticket) → `route`: **no LLM call**, no draft, `senior_support`, reason `prompt_injection`, evidence = detector names and scores. Earlier the gate ran in parallel with the LLM calls, so the attacker's text reached Claude before we knew; that was wrong. Cost: `risk_classifier` now runs before the LLM calls instead of beside them (~60 ms vs ~1–2 s of LLM time). Second line: the safety LLM also flags injection on tickets that passed the gate.
- **An exact phrase skips the gate** (2026-11-23): a pattern hit is certain, so `pattern_check → risk_classifier → route`; `injection_check` runs only on tickets the patterns found clean, to catch what they missed. The journey diagram then shows which layer caught the attack.
- **Measured trade-off of the any-signal rule:** the injection model alone false-alarms on ~3% of harmless tickets (dev 3/76, test 1/62, e.g. "Thanks a lot, everything works now!" at 0.52); those lose the AI draft. Accepted for Phase 1 (an agent writes the reply); Prompt Guard 2 is expected to improve precision, to be measured before any threshold change.
- **Trade-offs / caveats:** the training examples are ours, not real tickets: production retrains on agent-labelled tickets. The uniform 0.5 threshold was chosen after seeing held-out results (it is the classifier's natural decision point, not fitted), so a fresh test set (ideally real tickets) is the proper check. Weak signals have lower precision by design (recall first).


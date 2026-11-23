# Support-ticket co-pilot

A co-pilot for support agents at a retail forex/CFD broker. For every incoming ticket it:
1. **Classifies** it: category, urgency, language, sentiment.
2. **Drafts a reply** grounded in the knowledge base, **citing the pages used**, or **abstains** when the KB doesn't cover it.
3. **Routes** to a human reviewer whenever required, with reason codes.

It never talks to customers directly: an agent reviews and sends every reply.

## Status (2026-11-23)

| Part | State |
|---|---|
| Knowledge base: 4 pages → 26 citable chunks, flags for gaps/conflicts/defects, incremental ingestion | ✅ done |
| Retrieval: multilingual embeddings (bge-m3) + pgvector, evaluated (R@3 0.90, MRR 0.88, Arabic Hit@3 1.00) | ✅ done |
| PII masking before anything else sees the ticket: Presidio + GLiNER, encrypted vault, residual-PII monitor, evaluated | ✅ done |
| Tickets as background jobs: `POST /tickets` → ticket number, Postgres queue, worker(s), `GET /tickets/{number}` for status/progress/result, retries, crash recovery (D11) | ✅ done |
| Workflow: LangGraph graph (D3): pattern_check → risk_classifier ∥ classify_safety ∥ classify_triage → policy_gate → … | ✅ **pattern_check** (exact phrases, spam), **injection_check** (its own gate, before any LLM), **risk_classifier** (bge-m3 + logistic regression, 6 risks, D12) and **route** (reason codes → queue) implemented. Spam → a person at low priority; prompt injection (any signal) → **no LLM call, no AI draft**, senior support. The LLM calls, gate, draft and checks are placeholders (reported as such), so other tickets go to a person with reason `no_draft` |
| Agent API: inbox, ticket with draft/citations/why, audited customer details, send / edit / handle manually | ✅ done (local, no login; Phase 2: SSO) |
| Audit log (legal record): processing, PII reveals, agent decisions with AI draft AND final text, masked | ✅ done |
| Monitoring end to end: OpenTelemetry → Grafana (metrics, traces, logs), LangSmith for LLM calls | ✅ done |
| Prompt Guard 2 as a dedicated injection detector, LLM nodes, policy gate, end-to-end eval + model benchmark, business case with measured numbers | ⏳ next |

## Quick start

```bash
cp .env.example .env            # .env is the single source of truth (keys: see comments inside)
#   vault key: uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
uv run copilot keys new helpdesk helpdesk   # API keys: the key is shown once, add the printed
uv run copilot keys new sara agent          #   name:role:hash line to API_KEYS in .env
docker compose up --build       # db → indexer (KB + vectors) → app + worker, plus lgtm (monitoring)
docker compose up --scale worker=3   # more workers: they never take the same ticket
```

| What | Where |
|---|---|
| API (helpdesk + agent endpoints, `GET /health`) | http://localhost:8000/docs |
| Monitoring dashboard | http://localhost:3000 (admin/admin) → **Support Co-pilot** |
| LLM traces | smith.langchain.com → agent `support-copilot` → environment `local` |
| Graph (LangGraph Studio) | `uv run langgraph dev` → link printed in the terminal |
| Postman collection | `postman/support-copilot.postman_collection.json` |

**Local development** (code on your machine, services in Docker):
```bash
uv sync
docker compose up -d db lgtm
uv run copilot kb index               # ingestion: the only writer of the KB (D8)
uv run python -m copilot.main         # API on http://127.0.0.1:8000
uv run copilot worker                 # background worker (run in a second terminal; start more to scale)
COPILOT_HELPDESK_KEY=sk_... uv run copilot replay   # send the 76 eval tickets → watch the dashboard fill
uv run pytest -q                      # tests (Postgres tests skip if the db is down)
```

## API: only what the business uses (D5)
- `POST /tickets` `{id, subject, body, ...}` → **202** `{ticket_number, status: "queued", status_url}` in ~200 ms (PII masked, mapping stored encrypted, ticket queued). The same `id` again → **200** with the same number (webhooks are redelivered; nothing is queued twice).
- `GET /tickets/{number}` → where it stands: `status` (queued → processing → done | failed), `current_step`, **`steps_completed`** (real work only, e.g. `0/8`), **`complete`** and **`steps`** (each step with its own status: `completed`, or `placeholder` = not implemented, did nothing; placeholders produce nothing, so `routing`/`classification`/`reply` stay `null` instead of fake values), `attempts` (max 3), timings (`seconds_in_queue`, `seconds_processing`), the LangSmith `trace_id`, and when done the `result` (classification, reply, citations, routing). On errors: `error`, retried automatically, `failed` after 3 attempts.
- `GET /health` → used by Docker's healthcheck.

No response ever contains ticket text or a real PII value.

## Agent API (JSON)
What a support agent's frontend, or the helpdesk, calls, with an **agent API key** (see Security).

| Endpoint | What it returns / does |
|---|---|
| `GET /tickets?queue=&status=&limit=` | **Inbox**: counts by status + rows with status, progress (`5/8`), current step, category, urgency, the co-pilot's decision, reason codes, queue (support agent / senior support / compliance), PII types, handled or not |
| `GET /tickets/{number}` | **One ticket**: status and steps, the customer's message **masked**, classification, **draft reply**, **citations** (page title + URL + section), routing decision + reasons, the agent's decision, timings, trace ID |
| `GET /tickets/{number}/customer-details` | Placeholder → real value from the encrypted vault, for the agent handling the ticket. Cards, documents and accounts still only `**** 1111`. Agent keys only; every call is written to the audit log with the agent's name (from the key). |
| `POST /tickets/{number}/decision` | `{"action": "send", "final_text": "..."}` (recorded as `sent`, or `edited` if the draft changed) or `{"action": "manual"}`. Placeholders like `[NAME_1]` are filled in at send time; the final text is stored and audited **re-masked**, next to the AI draft (PRD §6b; the difference is the edit-rate metric). 409 if the co-pilot isn't done or the ticket was already handled. |

## Security
- **API keys with roles** (`Authorization: Bearer <key>`): `helpdesk` may create tickets and read their status; `agent` keys (one per person) may use the inbox, tickets, customer details and decisions. The agent's name comes from the key, so the audit log records who really looked or decided. `GET /health` is open (Docker).
- `.env` stores only SHA-256 hashes of the keys (`API_KEYS=name:role:hash,...`); keys are 32 random bytes from `copilot keys new`, shown once. No keys configured → every protected endpoint answers 401.
- **API docs** (`/docs`, `/redoc`, `/openapi.json`) only with `API_DOCS=true` (development); off in production.
- **Docker ports are bound to 127.0.0.1** (API, Postgres, Grafana, OTLP): nothing is reachable from the network in Phase 1.
- **Phase 2:** SSO (OIDC) for agents, signed webhooks or mTLS for the helpdesk, TLS at a gateway, key rotation via a secrets manager.

## How a ticket flows

```
POST /tickets  (API, ~200 ms)
   │
   ▼
mask PII (Presidio + GLiNER) ──► encrypted vault (placeholder → value, 30-day expiry)
   │  only "[CARD_1]", "[NAME_1]"... from here on
   ▼
tickets table (Postgres = the queue): status queued ──► 202 + ticket number
   │
   ▼  worker (separate process; claims with FOR UPDATE SKIP LOCKED; lease → crash recovery)
LangGraph:  pattern_check ─┬─ spam ─────────────────────────────────────────────► route (low priority)
                           └► injection_check ─► risk_classifier ─┬─ injection ───► route (no LLM, no draft)
                                                                  └► classify_safety ∥ classify_triage
             ─► policy_gate ─┬─► retrieve ─► draft ─► post_check ─┬─► compose ─► route ─► audit
                             │                ▲                   │
                             │                └── redraft once ───┘
                             └─► template_reply ───────────────────► compose
```
Each node's completion is saved, so `GET /tickets/{number}` shows live progress. Errors are retried (max 3), then `failed`; a ticket whose worker died is picked up again when its lease expires.

Hard rules are enforced by **code at fixed points**: `policy_gate` before any reply is written (trading advice, complaint/KYC/AML decisions never reach the drafter), `post_check` after (numbers not in the cited text, missing citations, advice wording). See [docs/DECISIONS.md](docs/DECISIONS.md) D3, D4.

## PII protection (D10)
- **Identifiers** by deterministic, checksum-validated patterns: cards (Luhn), IBAN, email, phones (Egypt/Gulf regions), passport and account numbers (context words in English and Arabic).
- **Names and street addresses in any language** by GLiNER-PII, a multilingual model plugged into Presidio, with measured rules on top (pronouns and relatives are never names; a city or country alone is not PII, the compliance check needs it).
- Masking runs **before the graph**, so raw PII never reaches the LLM, the graph state, LangSmith, logs or the audit log.
- **Measured** (`uv run copilot eval pii`): on 20 new multilingual cases, **22/22 values masked**, 24/25 non-PII words kept; English-only spaCy would mask 14/22. ~170 ms per ticket.
- **Monitored**: a residual check flags text that still looks like PII after masking (e.g. a mistyped card number that fails the checksum): WARNING log + metric + dashboard panel, alertable.

## Monitoring end to end (D6)

| Question | Tool |
|---|---|
| How many tickets, how fast, how many routed to a human, why? Queue backlog and wait? Possible PII misses? Errors? Cost? | **Grafana** dashboard *Support Co-pilot* (Prometheus metrics) |
| Which tickets came in, where is each one, who handled it? | **Grafana → Tickets** row: live table from Postgres (view `tickets_monitoring`, read-only role, no ticket text), status / agent-decision charts, edit rate, oldest waiting, failed |
| Where is *this* ticket now, with its draft? | `GET /tickets/{number}` (agent key) |
| What happened to *this* ticket, step by step, incl. database writes? | **Grafana → Tempo**: `POST /tickets` → `ticket.intake` → `pii_mask`; worker: `ticket.process` → nodes |
| What did the LLM see and answer, tokens and cost per call? | **LangSmith** (the OTel span carries `langsmith.trace_id`) |
| What was logged? | **Grafana → Loki**, or `uv run copilot logs -f` / `--grep <ticket_id>` / `--level WARNING` |
| Who saw customer details, who sent what, and what did the AI suggest? | **Audit log** `logs/audit.jsonl` |
| Masking summary across tickets | `uv run copilot pii-report` |

Instrumented with **OpenTelemetry**, so production only changes the endpoint: Grafana Cloud, a self-hosted LGTM stack, or Datadog. Nothing logs or traces ticket text or PII values: IDs, types, counts and timings only. The **audit log** (legal record) is separate and ours (PRD §6b).

**Phase 2 alerts:** residual PII suspects > 1% of tickets in 1 h → compliance · hard-rule violation > 0 → page · p95 latency > 10 s · cost per ticket over budget.

## Evaluation
| Eval | Command | Result |
|---|---|---|
| Retrieval (85 queries, 68 answerable, + 28 tickets) | `uv run copilot eval retrieval` | R@3 0.90 · MRR 0.88 · Hit@3 en 0.94 / ar 1.00 / other 1.00 |
| PII masking (20 new cases + 76 labelled tickets) | `uv run copilot eval pii` | value recall 1.00 · keep precision 0.96 · ticket type match 1.00 |
| Risk signals: patterns + classifier (62 held-out cases, 12 languages) | `uv run copilot eval risks` | recall 1.00 every risk and language · precision 0.84 · 0 strong false alarms · category 0.89 |
| End to end (classification, abstention, routing recall, hard-rule violations, cost) | `copilot eval tickets` | ⏳ needs the LLM nodes |

Details and trade-offs: [docs/BENCHMARK.md](docs/BENCHMARK.md). Test strategy: [eval/README.md](eval/README.md).

## Phases
- **Phase 1: ship ASAP.** API + background worker on one Postgres (queue, vault, KB, vectors), top-k retrieval, two LLM calls with deterministic gates, agents review every draft. Monitoring and PII protection from day one.
- **Phase 2: scale (~10,000 tickets/month, growing KB and SOPs).** Helpdesk webhook + private-note reply, more workers (the queue already scales horizontally), LISTEN/NOTIFY instead of polling, hybrid search + reranking, caching and model split, continuous evaluation from agent edits, Grafana Cloud / self-hosted monitoring with alerts, KMS for the vault key. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Mocks and stubs
- **Helpdesk:** `POST /tickets` stands in for the helpdesk webhook; Phase 2 posts the draft back as a private note.
- **Customer account lookup** (withdrawal status, balances): stubbed; tickets that need it route with `account_action_required`.
- **Human review queue:** the routing decision + destination in `GET /tickets/{number}`; Phase 2: helpdesk assignment.
- **Pushing the result to the helpdesk:** the helpdesk polls `GET /tickets/{number}`; Phase 2: the worker posts the draft as a private note (or a callback URL).

## What's where
| Document | What it answers |
|---|---|
| [PRD.md](PRD.md) | What we build: requirements, routing triggers, compliance |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it fits together, Phase 1 and Phase 2 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why: every decision with options and trade-offs (D1–D11) |
| [docs/BENCHMARK.md](docs/BENCHMARK.md) | Measured results |
| [docs/BUSINESS_CASE.md](docs/BUSINESS_CASE.md) | Is it worth running at 10,000 tickets/month, and with which configuration? |
| [knowledge_base/KB_FINDINGS.md](knowledge_base/KB_FINDINGS.md) | Gaps, overlaps and inconsistencies in the four help pages |

## How I built this
Built with **Claude Code** as the agentic coding tool:
- **Design first, code second.** Requirements, architecture and decisions were written down before code (`PRD.md`, `docs/`). I made the decisions; the agent challenged them and proposed options.
- **Project instructions for the agent** in [`.claude/CLAUDE.md`](.claude/CLAUDE.md): hard rules, engineering rules, how decisions are recorded.
- **Measure, then decide.** Every choice that could be measured was: embedding model (3 benchmarked), PII engine (GLiNER vs spaCy on new cases), thresholds from the eval sets.
- **Corrections along the way** (examples): moved PII masking in front of the graph so raw PII can't reach traces; removed API endpoints the business doesn't use; replaced name-score thresholds with measured rules after GLiNER tagged "friend" and "I" as people; found and pinned a protobuf/sentencepiece conflict.

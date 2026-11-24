# Support-ticket co-pilot

A co-pilot for support agents at a retail forex/CFD broker. For every incoming ticket it:
1. **Classifies** it: category, urgency, language, sentiment.
2. **Drafts a reply** grounded in the knowledge base, **citing the pages used**, or **abstains** when the KB doesn't cover it.
3. **Routes** to a human reviewer whenever required, with reason codes.

It never talks to customers directly: an agent reviews and sends every reply.

## Review in 10 minutes
1. **Run it** (Quick start below: copy `.env.example`, add your Claude key, `docker compose up`).
2. **Open the agent UI** at http://localhost:8000/ui (the sign-in link in the reviewer email opens it signed in). Create a ticket such as *"What is the minimum deposit?"*, then *"Should I buy gold now?"*, and see the draft, its cited help pages, the reasons and the routing.
3. **Run Postman** folder 7 (questions the help pages answer), then 4 to 6 (the hard rules), then 3 (every routing path). See [Postman](#postman).
4. **Read** [docs/BUSINESS_CASE.md](docs/BUSINESS_CASE.md) (one page: worth running, which configuration), then the evidence for each hard rule and for scale (the [document map](#document-map)).

## Status (2026-11-24): Phase 1 complete
| Part | State |
|---|---|
| Knowledge base: the 4 help pages used **as they are** → 26 citable chunks; gaps, overlaps and inconsistencies flagged ([KB_FINDINGS](knowledge_base/KB_FINDINGS.md)); incremental ingestion | ✅ |
| Retrieval: bge-m3 + pgvector (HNSW): Recall@3 0.90, MRR 0.88, Arabic Hit@3 1.00; same numbers on a clean `docker compose up` | ✅ |
| PII masked before anything else sees the ticket (Presidio + GLiNER), encrypted vault, audited reveal | ✅ |
| Tickets as background jobs: Postgres queue, workers, retries, crash recovery (lease), live progress | ✅ |
| Workflow: all **14 LangGraph steps** do real work (patterns → injection gate → risk classifier → 2 parallel Claude calls → validation → confidence → **hard rules in code** → retrieve → draft → post_check → compose → route → **audit**) | ✅ |
| Agent UI (`/ui`) and agent API: inbox, draft + citations + notes, fill in customer details, send / edit / manual | ✅ |
| Audit log (legal record): processing with the full decision record, PII reveals, agent decisions with AI draft and final text, all masked | ✅ |
| Monitoring: OpenTelemetry → Grafana (metrics, traces, logs), LangSmith for LLM calls | ✅ |
| End-to-end eval (86 tickets): release gate **PASS**, 0 hard-rule violations, must-route 59/59, advice declined 19/19, complaint/KYC/AML 15/15, **0 uncited numbers**, $0.0007/ticket | ✅ |
| Postman: 124 requests in 8 folders, all passing against a fresh stack | ✅ |
| Business case with measured configurations (C1/C2/C3) and Phase 2 scale tests on AWS | ✅ |

## Quick start
```bash
git clone https://github.com/mohabashraf/support-copilot.git && cd support-copilot
cp .env.example .env            # then put YOUR Claude key in .env:  ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build       # db → indexer (KB + vectors) → app + worker, plus lgtm (monitoring)
docker compose up --scale worker=3   # optional: more workers; they never take the same ticket
```
That is all that is required. **API keys:** `.env.example` holds only the *hashes* of two demo keys (a helpdesk key that creates tickets, an agent key "reviewer" that reads and decides). The plain keys come separately with the review (by email), together with a Postman collection that has them built in and a link that opens the agent UI signed in. A hash cannot be used to log in; the app logs a warning while the demo keys are in use. **Your own keys** (anything beyond a local review): `docker compose run --rm --no-deps -v "$PWD:/work" -w /work app copilot setup --replace-demo` writes new keys to `.support-copilot-keys` and `postman/local.postman_environment.json` (both gitignored; `.env` keeps only hashes). **Encryption key:** `PII_ENCRYPTION_KEY` may stay empty; the app then creates one on first start and keeps it in the `app-data` volume. A missing Claude key stops the app with one message saying what to set. Without a Hugging Face token the injection gate uses its measured no-token fallback model. **First start** downloads the models (≈ 2.5 GB) into a Docker volume: 3.5 min on a fresh cloud VM, longer on a slow connection; later starts reuse it.

| What | Where |
|---|---|
| **Agent UI**: create a ticket, inbox, draft + citations + reasons + notes, fill in customer details, send / handle manually | http://localhost:8000/ui |
| API docs (Swagger: every endpoint, try it out) | http://localhost:8000/docs |
| Monitoring dashboard | http://localhost:3000 (admin/admin) → **Support Co-pilot** |
| Postman (8 folders, 124 requests) | `postman/support-copilot.postman_collection.json`, or the keyed copy from the reviewer email |
| LLM traces | smith.langchain.com → agent `support-copilot` (only with `LANGSMITH_API_KEY`) |
| Graph (LangGraph Studio) | `uv run langgraph dev` → link printed in the terminal |

**Local development** (code on your machine, services in Docker):
```bash
uv sync
docker compose up -d db lgtm
uv run copilot kb index               # ingestion: the only writer of the KB (D8)
uv run python -m copilot.main         # API on http://127.0.0.1:8000
uv run copilot worker                 # background worker (second terminal; start more to scale)
uv run pytest -q                      # 290+ tests (Postgres tests skip if the db is down)
uv run copilot eval e2e               # the end-to-end eval with real models (~$0.06)
```

## Document map
| Read this | When you want to know |
|---|---|
| [PRD.md](PRD.md) | What we build: requirements, hard rules, routing triggers, compliance |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it fits together, Phase 1 and Phase 2 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why: every decision with the options and trade-offs (D1–D17) |
| [docs/BUSINESS_CASE.md](docs/BUSINESS_CASE.md) | **One page:** worth running at 10,000 tickets/month, and with which configuration |
| [docs/BUSINESS_CASE_DETAILS.md](docs/BUSINESS_CASE_DETAILS.md) | The assumptions and the calculation behind it |
| [docs/BENCHMARK.md](docs/BENCHMARK.md) | Measured results: C1/C2/C3 configurations, retrieval models, latency, cost |
| [docs/EVIDENCE_TRADING_ADVICE.md](docs/EVIDENCE_TRADING_ADVICE.md) | Proof for "never give trading advice" (19/19, 11 languages) |
| [docs/EVIDENCE_COMPLAINT_KYC_AML.md](docs/EVIDENCE_COMPLAINT_KYC_AML.md) | Proof for "never decide a complaint, KYC or AML outcome" (15/15) |
| [docs/EVIDENCE_GROUNDING.md](docs/EVIDENCE_GROUNDING.md) | Proof for "never invent policy, fees or timelines; cite or say unknown" (0 uncited numbers) |
| [docs/EVIDENCE_SCALE.md](docs/EVIDENCE_SCALE.md) | Clean install, retrieval and answers through Docker, Phase 2 volume and KB-growth tests (AWS) |
| [docs/HOW_TO_TRACE.md](docs/HOW_TO_TRACE.md) | Follow one ticket through every tool: API, Grafana, traces, logs, LangSmith, audit log |
| [knowledge_base/KB_FINDINGS.md](knowledge_base/KB_FINDINGS.md) | Gaps, overlaps and inconsistencies in the four help pages, and how each is handled |
| [knowledge_base/CHUNKS.md](knowledge_base/CHUNKS.md) | The 26 chunks: what each says, its flags, which questions it should answer |
| [eval/README.md](eval/README.md) | Test strategy: the eval sets, how they are scored, the release gate |
| `eval/results/` | Raw results of every eval run (`e2e_run9.json` = the reported run) |
| `eval/scale/` | The scripts behind EVIDENCE_SCALE (API answer test, KB growth, HNSW speed) |
| `postman/` | The collection and `run_collection.mjs` (runs it with Node, no Postman needed) |
| [.claude/CLAUDE.md](.claude/CLAUDE.md) | The instructions the coding agent worked under (see How I built this) |

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

## Agent UI (`/ui`)
One page served by the API itself (no build step, no extra service). Open the sign-in link from the review email (`/ui#helpdesk=…&agent=…`: the part after `#` stays in the browser, is never sent to the server, and is removed from the address bar), or paste the keys. Then:
- **create a ticket** as the helpdesk would, and watch it move through the 14 steps;
- **inbox** with status and queue filters (support agent / senior support / compliance), refreshed automatically;
- **one ticket**: the masked message, category / urgency / language / sentiment, the routing reasons, the co-pilot's **notes for the agent** (uncovered parts, page conflicts), the **editable draft** with its **cited help pages**, **Fill in customer details** (replaces `[NAME_1]`… with the real values so the agent reads exactly what the customer gets; cards stay `**** 1111`; logged in the audit log), and **Send** or **Handle manually** (recorded as sent / edited / manual, the stored copy masked again).

It uses the same JSON API as Postman with the caller's own keys, so it adds no new permissions. Monitoring (volumes, latency, routing, PII, cost) is the Grafana dashboard at http://localhost:3000.

## Postman
**Reviewer:** import the keyed collection from the review email (keys built in, nothing else to set) and run any folder with the Collection Runner. **Otherwise:** import `postman/support-copilot.postman_collection.json` and `postman/local.postman_environment.json` (written by `copilot setup --replace-demo`) and select that environment. Every request that creates a ticket waits for the worker and checks the result; details per ticket are in the Postman Console. Postman must reach `localhost`: use the desktop app, or the Desktop Agent on postman.com (otherwise it shows "Could not send request"). **Without Postman:** `node postman/run_collection.mjs [folder prefix ...]` runs the same requests and test scripts with Node only (keys from the environment file, the env vars `HELPDESK_KEY`/`AGENT_KEY`, or a keyed collection via `COLLECTION=path`). Last full run against a fresh stack: 124 requests, all passing.

| Folder | What it proves |
|---|---|
| 0–2 | Health; ticket intake (202, idempotent IDs, PII never echoed, 401/404/422); agent endpoints (inbox, masked ticket, audited reveal, 403 for the helpdesk key, decisions, 409 on a second decision) |
| 3 | **Every path through the checks**: spam, injection phrase, injection model, security phrase, risk flagged, clean question, spam + injection |
| 4 | Never give trading advice (19 requests + 2 controls, 7 languages) |
| 5 | Never decide a complaint, KYC or AML outcome (15 + 3 controls) |
| 6 | Never invent policy, fees or timelines: cite or say unknown (13) |
| 7 | **Positive scenarios: questions the help pages answer** (24, in en / ar / ru / fr / es / vi): answered, right page cited, KB fact, no uncited number |

## Edge cases we handle
| Case | What happens | Proven by |
|---|---|---|
| Spam: empty, emojis only, one character repeated, 3+ links, promotional phrase | No model and no LLM; to a person at **low** priority | Postman 3A, `tests/test_patterns.py` |
| Prompt injection: "ignore previous instructions", fake JSON output, role play, Arabic, Russian, new wording | Caught by patterns or the guard model **before any LLM reads the ticket**; fixed reply only; senior support | Postman 3B, 3C, 4G, 5E; `copilot eval injection` (18/18) |
| Trading advice: buy/sell, signals, price targets, leverage, disguised, mixed with a real question | Fixed polite decline in the customer's language; the factual part is still answered | Postman 4, `docs/EVIDENCE_TRADING_ADVICE.md` (19/19) |
| Complaint, KYC or AML decision asked (incl. via injection) | Neutral fixed acknowledgement; KYC/AML → compliance, complaints → senior | Postman 5, `docs/EVIDENCE_COMPLAINT_KYC_AML.md` (15/15) |
| Vulnerable customer ("lost all my savings") · angry / urgent | Personal acknowledgement, senior support · higher priority | Postman 3E, eval X25 / E15 |
| Security-sensitive (change email/phone, disable 2FA, account access) | Fixed security text, senior support | Postman 3D |
| Question the help pages don't answer (fees, spreads, withdrawal steps) | No reply drafted; a person answers, with a note "not covered by the help pages" | Postman 6A, eval abstain 10/10 |
| Partly covered | The cited part is answered; the rest is listed for the agent | Postman 6C, 7 (K13) |
| Customer states a wrong policy or number ("10 USD, right?", "guaranteed in 1 hour") | Never confirmed: the KB figure is given, or the draft is withheld and routed | Postman 6B |
| Help pages contradict each other ("instant" vs "60 minutes") | Answer drafted and flagged `kb_conflict` for the agent | Postman 6D, 7 (K06) |
| Defective, promotional or legal-notice KB text | Never used to answer | `tests/test_retrieve_draft.py` |
| Any number in a reply not in a cited chunk | Redraft once, then withheld; counted as a hard-rule violation by the eval | `post_check`, `copilot eval e2e` (0 in 43 drafts) |
| Account-specific question ("where is my withdrawal?") | Documented facts + `account_action_required` → an agent checks the account | eval X19, X33 |
| Many languages (Arabic incl. Egyptian/Gulf, Russian, French, Spanish, Vietnamese, Thai, Chinese …) | Reply in the customer's language | Postman 7E, eval reply language 80/80 |
| Low-resource language (Kurdish, Swahili) | English draft + a person who reads the language | eval KU1, SW1 |
| PII anywhere: cards (Luhn), IBAN, phones, emails, passports, names and addresses in any language | Masked **before** any model, trace or log; encrypted vault; cards only ever shown as `**** 1111`; a mistyped card is flagged as a possible miss | Postman 1.5, 1.6, 2.5; `copilot eval pii` |
| A message that is only PII (a card number) | Not spam, not injection; nothing to answer → a person | Postman 3F (F4) |
| Same ticket ID again (webhook redelivered) | Same ticket number, nothing queued twice | Postman 1.4 |
| Bad requests: no key / wrong role / unknown ticket / missing body | 401 / 403 / 404 / 422 | Postman 1.7–1.9, 2.4 |
| Agent decides twice, or before the co-pilot is done | 409 | Postman 2.7, `tests/test_agent_api.py` |
| Worker crashes mid-ticket | Lease expires, another worker resumes from the failed step; nothing lost or duplicated | `docs/EVIDENCE_SCALE.md` (172/172) |
| LLM timeout, rate limit, invalid output | Retried; after 3 attempts the ticket goes to a person (`technical_failure`) | `LLM_RETRY` in `pipeline/workflow.py`, `tests/test_tickets.py` |
| Classifier unsure | `low_confidence` → a person reviews | eval A01 |
| `.env` not filled in · gated guard model without a token | One startup message naming what is missing · measured no-token fallback guard | `services.require_secrets`, `services.load_guard` |

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
                           ├─ injection phrase ─────────► risk_classifier ─────► route (no LLM, no draft)
                           └► injection_check (model) ─► risk_classifier ─┬─ injection ───► route (no LLM, no draft)
                                                                  └► classify_safety ∥ classify_triage
             ─► policy_gate ─┬─► retrieve ─► draft ─► post_check ─┬─► compose ─► route ─► audit
                             │                ▲                   │
                             │                └── redraft once ───┘
                             └─► template_reply ───────────────────► compose
```
Each node's completion is saved, so `GET /tickets/{number}` shows live progress. Errors are retried (max 3), then `failed`; a ticket whose worker died is picked up again when its lease expires. **A retry resumes from the step that failed** (LangGraph Postgres checkpointer), so finished steps and LLM calls already paid for are not repeated.

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
| End to end (classification, abstention, routing recall, hard-rule violations, cost) | `copilot eval e2e` | ✅ gate PASS (run 9: 0 violations incl. 0 uncited numbers, must-route 59/59, $0.0007/ticket) |

Details and trade-offs: [docs/BENCHMARK.md](docs/BENCHMARK.md). Test strategy: [eval/README.md](eval/README.md).

## Phases
- **Phase 1: ship ASAP.** API + background worker on one Postgres (queue, vault, KB, vectors), top-k retrieval, two LLM calls with deterministic gates, agents review every draft. Monitoring and PII protection from day one.
- **Phase 2: scale (~10,000 tickets/month, growing KB and SOPs).** Helpdesk webhook + private-note reply, more workers (the queue already scales horizontally), LISTEN/NOTIFY instead of polling, hybrid search + reranking, caching and model split, continuous evaluation from agent edits, Grafana Cloud / self-hosted monitoring with alerts, KMS for the vault key. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Mocks and stubs
- **Helpdesk:** `POST /tickets` stands in for the helpdesk webhook; Phase 2 posts the draft back as a private note.
- **Customer account lookup** (withdrawal status, balances): stubbed; tickets that need it route with `account_action_required`.
- **Human review queue:** the routing decision + destination in `GET /tickets/{number}`; Phase 2: helpdesk assignment.
- **Pushing the result to the helpdesk:** the helpdesk polls `GET /tickets/{number}`; Phase 2: the worker posts the draft as a private note (or a callback URL).

## How I built this
Built with **Claude Code** as the agentic coding tool:
- **Design first, code second.** Requirements, architecture and decisions were written down before code (`PRD.md`, `docs/`). I made the decisions; the agent challenged them and proposed options.
- **Project instructions for the agent** in [`.claude/CLAUDE.md`](.claude/CLAUDE.md): hard rules, engineering rules, how decisions are recorded.
- **Measure, then decide.** Every choice that could be measured was: embedding model (3 benchmarked), PII engine (GLiNER vs spaCy on new cases), thresholds from the eval sets.
- **Corrections along the way** (examples): moved PII masking in front of the graph so raw PII can't reach traces; removed API endpoints the business doesn't use; replaced name-score thresholds with measured rules after GLiNER tagged "friend" and "I" as people; found and pinned a protobuf/sentencepiece conflict.

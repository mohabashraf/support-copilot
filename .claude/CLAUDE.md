# Support-ticket co-pilot: project instructions for the coding agent

## What this project is
A co-pilot for support agents at a retail forex/CFD broker: classify each ticket, draft a reply grounded in the knowledge base with page citations (or abstain), and route to a human when required. Phase 1 ships fast; Phase 2 scales to ~10,000 tickets/month with a growing KB.

Read first: `PRD.md` (requirements), `docs/ARCHITECTURE.md` (design), `docs/DECISIONS.md` (why), `knowledge_base/KB_FINDINGS.md` (KB gaps and inconsistencies).

## Hard rules (never break, in code or prompts)
1. Never give trading advice: no buy/sell recommendations, signals, price targets or leverage suggestions. Detect and decline politely.
2. Never decide a complaint, KYC or AML outcome. Draft a neutral acknowledgement and route to a person.
3. Never invent policy, fees or timelines. Cite the KB or say it is unknown. Holding replies contain no timelines.
4. The co-pilot never talks to customers. It only produces drafts for an agent.
5. PII is replaced with typed placeholders before any LLM call; the mapping never reaches the LLM or the logs.

## Commands
```bash
uv run pytest -q                              # tests (pgvector tests skip if the db is down)
uv run ruff format . && uv run ruff check .   # PEP 8 (hook + CI enforce it)
docker compose up -d db                       # database only, for local development
uv run copilot kb index                       # INGESTION: after any change to knowledge_base/ (D8)
uv run python -m copilot.main                 # server: http://127.0.0.1:8000/docs
uv run copilot worker                         # background worker (processes queued tickets)
uv run copilot search "minimum deposit"       # read-only search
uv run copilot eval retrieval                 # recall / precision / MRR; compare with eval/results/
docker compose up --build                     # whole stack: db → indexer → app + lgtm (Grafana :3000)
uv run copilot eval pii                       # PII masking eval (new cases + labelled tickets)
uv run copilot replay                         # send the eval tickets to the running API (dashboard demo)
uv run copilot logs -f | uv run copilot pii-report   # app logs / masking summary
```

## Where is X
| Need | File |
|---|---|
| Start the server | `src/copilot/main.py` (runs) → `server/app.py` `create_app()` (builds) |
| CLI commands | `src/copilot/cli.py` |
| Settings | `.env` (truth) → `src/copilot/config.py` (types, no defaults) |
| Load KB + retriever (read-only) | `src/copilot/services.py` |
| HTML → chunks, IDs, flags | `kb/parse.py`, `kb/ingest.py`, `knowledge_base/kb_config.toml` |
| Chunk / KB models, hashes | `kb/models.py` |
| KB in Postgres | `kb/store.py` |
| Postgres connection pool | `src/copilot/db.py` (`open_pool`, shared by store + index) |
| Embed + write vectors (the only writer) | `retrieval/indexer.py` |
| Vector index (pgvector / memory) | `retrieval/index.py` |
| Search | `retrieval/search.py` |
| Retrieval metrics | `eval/metrics.py`, `eval/retrieval.py`, data in `eval/*.jsonl` |
| HTTP routes | `server/routes/` |
| Logging | `src/copilot/log.py` |
| Ticket workflow (LangGraph) + `process_ticket` | `src/copilot/pipeline/workflow.py` |
| PII masking (Presidio + GLiNER) / encrypted vault | `pipeline/pii.py` / `pipeline/pii_vault.py` |
| Data models (ticket, LLM outputs, result) | `pipeline/schemas.py` |
| Ticket API: helpdesk `POST /tickets` + agent endpoints (inbox, ticket, customer-details, decision) | `server/routes/tickets.py` |
| API keys with roles (helpdesk / agent), `copilot keys new` | `server/auth.py` |
| Audit log (append-only JSONL) | `src/copilot/audit.py` |
| Queue (Postgres) / intake + processing / worker loop | `tickets/store.py` / `tickets/service.py` / `tickets/worker.py` |
| LangSmith tracing / OpenTelemetry → Grafana | `src/copilot/tracing.py` / `src/copilot/telemetry.py` |
| Grafana dashboard | `observability/grafana/dashboards/support-copilot.json` |
| PII eval | `src/copilot/eval/pii.py`, data `eval/pii_cases.jsonl` |

## Gotchas
- After changing `knowledge_base/` run `copilot kb index`: running servers see it at once (no KB copy in memory, D9). A starting server refuses a model with no vectors.
- Search hits carry their chunk (one JOIN, D9): never look a hit up elsewhere (`kb.get`) or text and vector can come from different versions.
- The drafter gets top-k chunks only (D1 revised), never the whole KB.
- Adding a setting: `.env.example` + `config.py` + your `.env`, all three (skill `add-setting`).
- e5 embedding models need the `query: ` / `passage: ` prefixes (`retrieval/embeddings.py` `MODELS`).
- Imports go at the top (Ruff PLC0415). The only exception is `sentence_transformers` (loads torch, ~9 s).
- Never log ticket or query text (PII): log lengths, IDs, scores and timings. Same for traces and metrics (no ticket IDs as metric labels: cardinality).
- PII is masked BEFORE the graph (`process_ticket`): never pass raw ticket text into `graph.invoke`.
- The API exposes only the ticket endpoints and `GET /health` (D5): JSON only, no HTML, no debug endpoints.
- Responses show masked text; real values only through the audited customer-details endpoint; final texts are stored re-masked.
- Every endpoint except `/health` requires `Depends(require(<roles>))`; never trust a client-sent identity (the agent comes from the key). `.env` holds key hashes only.
- Workflow nodes must be idempotent: a ticket re-runs from the first node after an error or a worker crash (D11).
- The `tickets` and `pii_vault` tables are NOT derived data: never drop them on a schema change.
- Vector tables are derived data: on a schema change drop them and re-run `kb index`. Not true for future audit tables.
- Colima has no BuildKit: no `RUN --mount` in the Dockerfile.

## Skills and agents in this repo
- Skill `kb-update`: add or change a help page / FAQ / flag, end to end.
- Skill `add-setting`: add a new setting the `.env`-first way.
- Agent `hard-rules-reviewer`: run it on any change to prompts, routing, the workflow or reply templates.

## Engineering rules
- Python 3.12, `uv` for dependencies, type hints, Pydantic models for all data crossing a boundary.
- **PEP 8, enforced by Ruff** (config in `pyproject.toml`, line length 99). Run `uv run ruff format . && uv run ruff check .` before committing; the pre-commit hook and CI refuse unformatted code. Never disable a rule inline without a comment saying why.
- LangGraph is a **workflow** (fixed nodes + conditional edges), not autonomous multi-agents.
- Deterministic checks are code, not prompts. LLM output is always schema-validated.
- Model IDs, thresholds, categories and language tiers live in config, never hard-coded.
- KB pages are used **as they are**: never edit `knowledge_base/raw/` or rewrite KB text; flag problems instead.
- Every new behaviour gets a unit test; changes to prompts or models are checked against the eval set (`copilot eval`). Hard-rule violations must stay at 0.
- No secrets in the repo: keys come from `.env` (see `.env.example`).
- Only `copilot kb index` (retrieval/indexer.py) writes the KB or vectors; the server and search are read-only (D8).
- `.env` is the single source of truth for settings: no defaults in `config.py`; every new setting goes in `.env.example` too (a test checks it).
- Keep commits small, with clear messages.

## How we work
- The owner (Mohab) makes design decisions; record each one in `docs/DECISIONS.md` (context → options → decision → why → trade-offs).
- Explain non-obvious code so the owner can defend every line in a technical walkthrough.

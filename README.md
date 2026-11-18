# Support-ticket co-pilot

A co-pilot for support agents at a retail forex/CFD broker. For every incoming ticket it:
1. **Classifies** it: category, urgency, language, sentiment.
2. **Drafts a reply** grounded in the knowledge base, **citing the pages used**, or **abstains** when the KB doesn't cover it.
3. **Routes** to a human reviewer whenever required, with reason codes.

It never talks to customers directly: an agent reviews and sends every reply.

> Status: in progress. Sections marked _TBD_ are filled as the build progresses.

## Quick start

**Option A: everything in Docker (one command)**
```bash
cp .env.example .env          # settings: .env is the single source of truth
docker compose up --build     # db → indexer (builds the KB + vectors) → app
```
Then open http://localhost:8000/docs. The first run downloads the embedding model (~0.5 GB, cached in a volume).
The `indexer` runs `copilot kb index` and exits; the `app` starts only if it succeeded (D8: the server is read-only).
After changing the pages or `knowledge_base/kb_config.toml`: `docker compose run --rm indexer` (only changed chunks are re-embedded).

**Option B: local development (code runs on your machine, only the database in Docker)**
```bash
uv sync
cp .env.example .env
docker compose up -d db
uv run copilot kb index                 # ingestion: the only writer of the KB (D8)
uv run python -m copilot.main           # server on http://127.0.0.1:8000
uv run copilot search "minimum deposit" # CLI, read-only
uv run copilot eval retrieval           # recall / precision / MRR (eval/)
```
No Docker at all: set `VECTOR_STORE=memory` in `.env` (everything is built in memory from the raw pages).

## What's where
| Document | What it answers |
|---|---|
| [PRD.md](PRD.md) | What we build: functional and non-functional requirements, routing triggers, compliance |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it fits together: Phase 1 LangGraph workflow, Phase 2 scaled system |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why: every design decision with options and trade-offs |
| [docs/BENCHMARK.md](docs/BENCHMARK.md) | Measured results per model configuration |
| [docs/BUSINESS_CASE.md](docs/BUSINESS_CASE.md) | Is it worth running at 10,000 tickets/month, and with which configuration? |
| [knowledge_base/KB_FINDINGS.md](knowledge_base/KB_FINDINGS.md) | Gaps, overlaps and inconsistencies found in the four help pages, and how each is handled |

## Phases
- **Phase 1: ship ASAP** (git tag `v1-ship`): _TBD_
- **Phase 2: scale up** (git tag `v2-scale`): _TBD_

## Mocks and stubs
_TBD_ (helpdesk via JSONL, customer account lookup stubbed, review queue as an output field: see PRD §9)

## How I built this
Built with **Claude Code** as the agentic coding tool. The way of working:
- **Design first, code second.** Requirements, architecture and every design decision were worked out and written down before any code (`PRD.md`, `docs/`). I made the decisions; the agent challenged them and proposed options.
- **Project instructions for the agent** live in [`.claude/CLAUDE.md`](.claude/CLAUDE.md): the hard rules, engineering rules, and how decisions are recorded, so every coding session starts from the same constraints.
- **Evaluation as the safety net.** Every change to prompts, models or rules is checked against the eval set; hard-rule violations must stay at 0.
- _TBD: notes on what worked well with the agent and where I had to correct it._

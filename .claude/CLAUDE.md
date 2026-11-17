# Support-ticket co-pilot: project instructions for the coding agent

## What this project is
A co-pilot for support agents at a retail forex/CFD broker: classify each ticket, draft a reply grounded in the knowledge base with page citations (or abstain), and route to a human when required. Phase 1 ships fast; Phase 2 scales to ~10,000 tickets/month with a growing KB.

Read first: `PRD.md` (requirements), `docs/ARCHITECTURE.md` (design), `docs/DECISIONS.md` (why), `kb/KB_FINDINGS.md` (KB gaps and inconsistencies).

## Hard rules (never break, in code or prompts)
1. Never give trading advice: no buy/sell recommendations, signals, price targets or leverage suggestions. Detect and decline politely.
2. Never decide a complaint, KYC or AML outcome. Draft a neutral acknowledgement and route to a person.
3. Never invent policy, fees or timelines. Cite the KB or say it is unknown. Holding replies contain no timelines.
4. The co-pilot never talks to customers. It only produces drafts for an agent.
5. PII is replaced with typed placeholders before any LLM call; the mapping never reaches the LLM or the logs.

## Engineering rules
- Python 3.12, `uv` for dependencies, type hints, Pydantic models for all data crossing a boundary.
- LangGraph is a **workflow** (fixed nodes + conditional edges), not autonomous multi-agents.
- Deterministic checks are code, not prompts. LLM output is always schema-validated.
- Model IDs, thresholds, categories and language tiers live in config, never hard-coded.
- KB pages are used **as they are**: never edit `kb/raw/` or rewrite KB text; flag problems instead.
- Every new behaviour gets a unit test; changes to prompts or models are checked against the eval set (`copilot eval`). Hard-rule violations must stay at 0.
- No secrets in the repo: keys come from `.env` (see `.env.example`).
- Keep commits small, with clear messages.

## How we work
- The owner (Mohab) makes design decisions; record each one in `docs/DECISIONS.md` (context → options → decision → why → trade-offs).
- Explain non-obvious code so the owner can defend every line in a technical walkthrough.

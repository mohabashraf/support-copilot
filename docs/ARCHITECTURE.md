# Architecture (v0.2)

> Diagrams are written in **Mermaid**. They render automatically on GitHub. In VS Code, install the extension **"Markdown Preview Mermaid Support"** (`bierner.markdown-mermaid`) and open the preview with `Cmd+Shift+V`.

**Contents**
1. [System context](#1-system-context)
2. [Volume and what it implies](#2-volume-and-what-it-implies)
3. [Phase 1: components](#3-phase-1-components)
4. [Phase 1: the LangGraph workflow](#4-phase-1-the-langgraph-workflow)
5. [One ticket, step by step (sequence)](#5-one-ticket-step-by-step)
6. [Routing decision](#6-routing-decision)
7. [Knowledge-base ingestion](#7-knowledge-base-ingestion)
8. [Data flow and PII boundary](#8-data-flow-and-pii-boundary)
9. [Evaluation and benchmark pipeline](#9-evaluation-and-benchmark-pipeline)
10. [Phase 2: scaled system](#10-phase-2-scaled-system)
11. [Phase 2: ticket lifecycle with the helpdesk](#11-phase-2-ticket-lifecycle-with-the-helpdesk)
12. [Phase 1 → Phase 2: what changes](#12-phase-1--phase-2-what-changes)
13. [Key design choices](#13-key-design-choices)
14. [Code layout](#14-code-layout)

---

## 1. System context

Who and what the co-pilot talks to. It **never talks to customers**: agents send every reply.

```mermaid
flowchart LR
    Customer(["Customer<br/>email / chat / form"])
    Helpdesk["Helpdesk<br/>(Freshdesk / Zendesk)<br/>Phase 1: JSONL mock"]
    Copilot["Support co-pilot<br/>(this system)"]
    Agent(["Support agent"])
    Senior(["Senior support /<br/>Compliance"])
    LLM["LLM provider<br/>(Claude API)"]
    KB[("Knowledge base<br/>4 help pages")]
    Lead(["Support lead"])

    Customer -->|writes ticket| Helpdesk
    Helpdesk -->|new ticket| Copilot
    Copilot -->|"masked text only<br/>(PII placeholders)"| LLM
    KB --> Copilot
    Copilot -->|"private note: draft + citations,<br/>tags, priority, group"| Helpdesk
    Helpdesk --> Agent
    Helpdesk --> Senior
    Agent -->|reviews, edits, sends| Customer
    Copilot -->|metrics, KB gap report| Lead
```

---

## 2. Volume and what it implies

| | Calculation | Result |
|---|---|---|
| Per month | given | 10,000 |
| Per day | 10,000 ÷ 30 | ≈ 330 |
| Per hour (average) | 330 ÷ 24 | ≈ 14 |
| Peak hour | ≈ 5× average (business hours) | ≈ 70 |
| Peak per minute | 70 ÷ 60 | ≈ 1–2 |

Throughput is not the problem; **reliability and safety are**. Phase 1 processes tickets directly; Phase 2 adds a simple queue for retries and spikes. No Kafka.

---

## 3. Phase 1: components

```mermaid
flowchart TB
    subgraph Inputs
        JSONL["tickets.jsonl"]
        CLIIN["CLI: one ticket"]
        WEBIN["Web: submit ticket"]
    end

    subgraph App["Co-pilot service (Python)"]
        Adapter["Helpdesk adapter<br/>(file mock)"]
        Core["Co-pilot core<br/>LangGraph workflow"]
        KBStore["KB store<br/>chunks + flags + page URLs"]
        Vec["Vector index<br/>pgvector (Postgres)<br/>or in-memory for tests<br/>multilingual embeddings"]
        Rules["Rules & config<br/>categories, thresholds,<br/>language tiers, templates"]
        LLMIF["LLM interface<br/>(provider-agnostic)"]
        Audit["Audit log<br/>append-only JSONL"]
        Trace["Tracing<br/>Langfuse (optional)"]
    end

    subgraph Outputs
        Results["results.jsonl"]
        CLIOUT["CLI view"]
        Web["Web app: ticket view,<br/>KB explorer, search playground"]
    end

    Claude["Claude API"]

    JSONL --> Adapter
    CLIIN --> Adapter
    WEBIN --> Adapter
    Adapter --> Core
    KBStore --> Core
    Vec --> Core
    Rules --> Core
    Core <--> LLMIF
    LLMIF <--> Claude
    Core --> Audit
    Core -.-> Trace
    Core --> Results
    Core --> CLIOUT
    Core --> Web
    KBStore --> Web
    Vec --> Web
```

---

## 4. Phase 1: the LangGraph workflow

A **workflow (state machine)**, not autonomous multi-agents: two LLM nodes, deterministic nodes around them, conditional edges for routing (decision D3). Rectangles = code, hexagons = LLM calls.

```mermaid
flowchart TD
    Start([ticket in]) --> PII["pii_mask<br/>(code)<br/>typed placeholders,<br/>rule flags,<br/>ticket wrapped as DATA"]
    PII --> CLS{{"classify<br/>(LLM #1, structured JSON)<br/>intents[] · urgency · language<br/>reply_language · sentiment ·<br/>safety flags · confidence"}}
    CLS -->|invalid JSON| RETRY["retry once<br/>(code)"]
    RETRY --> CLS
    CLS --> SAFE["safety_check<br/>(code + flags)<br/>vulnerable_customer?<br/>security_sensitive?"]
    SAFE -->|vulnerable| CARE["care_note<br/>(code)<br/>short caring acknowledgement,<br/>no product talk,<br/>critical → senior_support"]
    SAFE -->|security| SEC["template_reply<br/>(code)<br/>secure-process reply,<br/>reveal nothing"]
    SAFE -->|none| GATE["policy_gate per intent<br/>(code)"]
    CARE --> ROUTE
    SEC --> ROUTE

    GATE -->|"blocked:<br/>trading advice"| TPL["template_reply<br/>(code)<br/>pre-approved decline"]
    GATE -->|"blocked:<br/>complaint / KYC / AML"| TPL2["template_reply<br/>(code)<br/>neutral acknowledgement"]
    GATE -->|allowed| DRAFT{{"draft<br/>(LLM #2)<br/>allowed intents only,<br/>grounded in KB, cites pages,<br/>or ABSTAINS, in reply_language"}}
    GATE -->|"mixed: some intents<br/>allowed, some blocked"| DRAFT
    DRAFT --> MERGE["merge<br/>(code)<br/>drafted parts + templates<br/>in intent order"]
    MERGE --> POST

    POST["post_check<br/>(code)<br/>digits normalised (٨→8)<br/>citations exist?<br/>numbers in cited text?<br/>no advice language?"]
    POST -->|"fail (1st)"| DRAFT
    POST -->|pass| JUDGE{{"groundedness_judge<br/>(LLM, optional,<br/>different model)"}}
    POST -->|"fail (2nd)"| ROUTE
    JUDGE --> ROUTE

    TPL --> ROUTE
    TPL2 --> ROUTE
    ROUTE["route<br/>(code)<br/>auto_draft_ok | human_required<br/>+ reason codes + destination"]
    ROUTE --> OUT([result + audit record])
```

### Human-in-the-loop for account actions (LangGraph `interrupt`)

When a ticket needs the customer's account checked (`account_action_required`, e.g. "my approved withdrawal never arrived"), the workflow **pauses**, saves its state, waits for the agent, then **resumes** where it stopped.

```mermaid
flowchart TD
    GATE["policy_gate<br/>(code)"] -->|needs_account_action| LOOK["account_lookup<br/>(code)<br/>Phase 1: stub → not available<br/>Phase 2: read-only back-office API"]
    LOOK -->|facts found| DRAFT{{"draft<br/>(LLM #2)<br/>KB + account facts"}}
    LOOK -->|not available| INT[/"interrupt<br/>(LangGraph)<br/>state saved by checkpointer<br/>agent asked for facts"/]
    INT -->|"agent submits facts<br/>e.g. sent 17 Nov, ref ABC"| RES["resume<br/>(same thread ID)"]
    RES --> DRAFT
    INT -->|agent skips| HOLD["holding reply<br/>human_required"]
    DRAFT --> POST["post_check<br/>agent facts cited as<br/>'agent-provided', not KB"]
    POST --> ROUTE["route"]
```

```mermaid
sequenceDiagram
    autonumber
    participant G as LangGraph workflow
    participant C as Checkpointer
    participant Ag as Agent (web page)
    participant L as Claude API
    G->>G: classify: withdrawal, needs_account_action = true
    G->>G: account_lookup (stub): not available
    G->>C: save state (thread T-1005)
    G-->>Ag: interrupt: "Please add the transaction status"
    Ag->>G: resume(T-1005, "Sent 17 Nov via bank transfer, ref ABC123")
    C-->>G: restore state
    G->>L: draft(KB + agent-provided facts)
    L-->>G: reply citing KB page + "agent-provided"
    G->>G: post_check, route, audit (agent ID + facts logged)
```

**Graph state** (one object passed between nodes):

```mermaid
classDiagram
    class CopilotState {
        Ticket ticket
        str masked_text
        PIIMap pii_map
        list rule_flags
        Classification classification
        GateDecision gate_decision
        dict account_facts
        str agent_id
        list retrieval_scores
        str draft
        list citations
        bool abstained
        list post_check_results
        int attempts
        Route route
        list reason_codes
        Usage usage
    }
    class Classification {
        str category
        str urgency
        str language
        list intents
        str sentiment
        str reply_language
        str decision_requested
        bool needs_account_action
        bool vulnerable_customer
        bool security_sensitive
        float confidence
    }
    class Citation {
        str page_title
        str page_url
        str section
        str chunk_id
    }
    class Usage {
        int tokens_in
        int tokens_out
        int tokens_cached
        float cost_usd
        float latency_s
    }
    CopilotState --> Classification
    CopilotState --> Citation
    CopilotState --> Usage
```

`pii_map` is never sent to the LLM or written to logs.

---

## 5. One ticket, step by step

Example: *"What is the minimum deposit, and what are your fees?"* (partly covered: minimum yes, fees no).

```mermaid
sequenceDiagram
    autonumber
    participant H as Helpdesk (JSONL mock)
    participant G as LangGraph workflow
    participant V as Vector index
    participant L as Claude API
    participant A as Audit log
    participant Ag as Agent

    H->>G: ticket T-1001
    G->>G: pii_mask (no PII), rule flags: none
    G->>L: classify(masked text)
    L-->>G: category deposit, urgency low, language en, confidence 0.93
    G->>G: policy_gate: allowed
    G->>V: embed ticket, score against KB chunks
    V-->>G: best chunk "deposit min-deposit", score 0.81
    G->>L: draft(top-k retrieved chunks, labelled + ticket)
    L-->>G: "Minimum deposit starts from 8 USD [How to deposit]. Fees: not covered."
    G->>G: post_check: "8 USD" in cited chunk, citation has page URL
    G->>G: route: auto_draft_ok + flag "partial: fees not in KB"
    G->>A: append record (draft, citations, versions, cost)
    G-->>Ag: draft + citations + "fees unknown, please answer"
    Ag->>H: edits fee part, sends reply
    H->>A: final sent text (Phase 2 via webhook)
```

---

## 6. Routing decision

Any trigger fires → `human_required` with its reason code (PRD FR-5.1).

```mermaid
flowchart TD
    S([after workflow]) --> T0{"vulnerable customer?"}
    T0 -->|yes| H0["human_required<br/>vulnerable_customer<br/>critical → senior_support"]
    T0 -->|no| T0b{"security-sensitive?"}
    T0b -->|yes| H0b["human_required<br/>security_sensitive<br/>→ senior_support"]
    T0b -->|no| T1{"hard-rule topic?<br/>trading advice · complaint · AML ·<br/>decision_requested ≠ none"}
    T1 -->|yes| H1["human_required<br/>hard_rule_topic /<br/>multi_intent_blocked<br/>→ strictest destination"]
    T1 -->|no| T8{"technical failure?"}
    T8 -->|yes| H8["human_required<br/>technical_failure"]
    T8 -->|no| T2{"abstained?"}
    T2 -->|yes| H2["human_required<br/>not_in_kb"]
    T2 -->|no| T3{"best retrieval score<br/>below threshold?"}
    T3 -->|yes| H3["human_required<br/>weak_retrieval"]
    T3 -->|no| T4{"confidence<br/>below threshold?"}
    T4 -->|yes| H4["human_required<br/>low_confidence"]
    T4 -->|no| T5{"post-check or<br/>judge failed?"}
    T5 -->|yes| H5["human_required<br/>post_check_failed"]
    T5 -->|no| T6{"KB conflict on<br/>the point asked?"}
    T6 -->|yes| H6["human_required<br/>kb_conflict"]
    T6 -->|no| T7{"angry + high urgency?<br/>low-resource language?"}
    T7 -->|yes| H7["human_required<br/>angry_urgent /<br/>low_resource_language"]
    T7 -->|no| T9{"needs account action<br/>and no agent facts yet?"}
    T9 -->|yes| H9["human_required<br/>account_action_required<br/>→ interrupt, agent adds facts"]
    T9 -->|no| OK["auto_draft_ok<br/>→ support_agent reviews and sends"]
```

In code, all triggers are evaluated and **every** reason code that fires is recorded (not only the first), so metrics count each cause.

---

## 7. Knowledge-base ingestion

Pages are used **as they are** (never edited); problems are flagged, and how the co-pilot handles each one is a documented choice.

**Ingestion and serving are separate (D8).** `copilot kb index` is the only writer; the server only reads.

```mermaid
flowchart LR
    subgraph ING["INGESTION: copilot kb index (offline job, the only writer)"]
        RAW[("knowledge_base/raw/*.html<br/>unchanged")] --> EXT["extract text<br/>+ headings"]
        EXT --> CH["chunk by section / FAQ<br/>keep page title + URL"]
        CH --> DD["deduplicate shared FAQ<br/>one chunk, both page URLs"]
        DD --> FL["flag chunks<br/>defective · conflict ·<br/>rule-sensitive"]
        FL --> DIFF{"per-chunk content hash<br/>vs what is stored"}
        DIFF -->|"new / changed only"| EMB["embed<br/>multilingual model"]
        DIFF -->|"gone"| DEL["delete"]
    end
    subgraph PG["Postgres"]
        VEC[("chunk_vectors_&lt;model&gt;<br/>vector + content hash + flags")]
        KBT[("kb_chunks<br/>whole chunk as JSON")]
    end
    EMB --> VEC
    DEL --> VEC
    DIFF -->|"written last"| KBT
    subgraph SRV["SERVING: server / CLI search (read-only)"]
        SEARCH["search: ONE JOIN<br/>vector + chunk, same version<br/>→ top-k for the draft prompt"]
        EXP["KB explorer + /health<br/>read kb_chunks per request"]
    end
    KBT --> SEARCH
    VEC --> SEARCH
    KBT --> EXP
```

Adding one page to a 1,000-chunk KB embeds only that page's chunks; an unchanged KB embeds nothing. `VECTOR_STORE=memory` (tests, no-Docker demo) builds everything in memory from the raw pages instead.

**Chunk kinds** (`BlockKind` in `kb/models.py`, what part of the page a chunk was cut from; each has a rule in `kb/parse.py`): `intro` · `steps` · `faq` · `promo` · `notice`.

**Flags** (`Flag` in `kb/models.py`): set by a human in `kb_config.toml [flags]`, reason recorded in `KB_FINDINGS.md`. Both lists are **closed on purpose**: an unknown value fails ingestion, so a new flag can't enter the KB without the workflow rule that handles it. Handling below is enforced by the ticket workflow (§4).

| Flag | Example | Handling |
|---|---|---|
| `conflict` | "instant" vs "within 60 minutes"; withdrawals "instant" vs "pending" queue | conservative statement + `kb_conflict` reason code to the agent |
| `defective` | withdraw steps copy-pasted from deposit ("amount to deposit", "Start Trading!") | kept, visible in explorer, **never used for an answer** |
| `rule_sensitive` | "What leverage should I select?", "Why was my withdrawal rejected?" | may state documented facts (max 1:2000), never suggest a setting or decide an outcome |
| `compliance` | access notice: who may use the site | never interpreted for a customer; eligibility questions → `aml_compliance` → compliance team |
| `gap_link` | "Click here" / "see account types" to a page not in the KB | answer only what the chunk itself says; missing part → partial answer + flag (FR-3.2b) |
| `route` | "I have not received my funds…" (KB says contact support) | draft the KB guidance, route to a human (`account_action_required`) |
| `promo` | "Trade from every device" | never cited as an answer |

Not flags: a **duplicate** FAQ (same question on deposit and withdraw pages) is stored once with both page URLs; a **gap** (fees, payment methods, withdrawal limits) has no chunk at all → abstain → `not_in_kb`.

**Adding a kind or flag (Phase 1):** add the value to the Literal in `kb/models.py`, add its handling (parse rule for a kind; workflow rule for a flag), a test, and a row here. Content changes that reuse existing kinds and flags (new pages, new FAQs, edited text) need no code change; see `.claude/skills/kb-update`.

---

## 8. Data flow and PII boundary

What leaves our service, and in what form.

```mermaid
flowchart LR
    subgraph Ours["Inside our service (trusted boundary)"]
        T["ticket<br/>(raw, with PII)"] --> M["pii_mask"]
        M --> MAP[("PII map<br/>CARD_1 → value<br/>memory / encrypted,<br/>short-lived")]
        M --> MT["masked text"]
        MT --> CORE["workflow"]
        CORE --> AUD[("audit log<br/>masked text + hash,<br/>draft AND final")]
    end

    subgraph External
        LLM["Claude API<br/>no training on data,<br/>zero retention where available"]
        LF["Langfuse<br/>Phase 1: free cloud, synthetic only<br/>Phase 2: self-hosted"]
    end

    CORE -->|"masked text only"| LLM
    CORE -.->|"masked traces"| LF
```

The PII map **never** crosses the boundary: it is not sent to the LLM, not traced, not logged in clear text.

---

## 9. Evaluation and benchmark pipeline

```mermaid
flowchart LR
    SET[("eval/tickets_labelled.jsonl<br/>expected category, urgency,<br/>language, must-route,<br/>abstain, required facts")] --> RUN["eval runner<br/>per configuration<br/>C1 · C2 · C3 · C4 · C2+J"]
    CFG["configs<br/>models, effort,<br/>thresholds"] --> RUN
    RUN --> WF["LangGraph workflow"]
    WF --> SC["scorers<br/>exact-match fields,<br/>post-check results,<br/>LLM judge: faithfulness,<br/>tone, language"]
    SC --> GATES{"release gates<br/>violations = 0<br/>must-route recall = 100%<br/>abstention ≥ 95%<br/>citations ≥ 95%"}
    GATES -->|pass / fail| REP["docs/BENCHMARK.md<br/>+ cost per ticket / month"]
    REP --> BC["docs/BUSINESS_CASE.md<br/>usable-draft rate,<br/>measured cost"]
```

---

## 10. Phase 2: scaled system

```mermaid
flowchart TB
    Customer(["Customer"]) --> HD["Helpdesk<br/>(Freshdesk / Zendesk)"]
    HD -->|"1. webhook (HTTPS + secret)"| API["Ingest API (FastAPI)<br/>verify secret · validate ·<br/>dedupe by ticket ID · 200 fast"]
    API -->|2. enqueue| Q[["Queue (Redis)"]]
    Q -->|3. consume| W1["Worker 1<br/>co-pilot core"]
    Q --> W2["Worker N<br/>co-pilot core"]
    Q -->|"failed 3×"| DLQ[["Dead-letter queue<br/>→ tag 'manual handling'"]]

    subgraph Shared["Shared services"]
        RET["Retrieval service<br/>hybrid: vector + BM25,<br/>RRF merge, reranker,<br/>versioned KB + SOPs"]
        CACHE[("Answer cache<br/>repeat questions")]
        DB[("Postgres<br/>audit log (insert-only),<br/>ticket status, idempotency keys")]
        ING["KB ingestion pipeline<br/>change detection,<br/>conflict report"]
    end

    W1 --> RET
    W1 --> CACHE
    W1 --> DB
    W2 --> RET
    W2 --> DB
    ING --> RET

    W1 -->|4. result| OUTQ[["Outbox queue"]]
    W2 --> OUTQ
    OUTQ --> WR["Helpdesk writer<br/>retries, respects<br/>API rate limits"]
    WR -->|"5. REST API: private note,<br/>tags, priority, group"| HD

    subgraph Obs["Observability"]
        OTEL["OpenTelemetry collector<br/>(PII filter)"]
        LF["Langfuse (self-hosted)"]
        MET["Metrics + alerts<br/>violations, abstain rate,<br/>latency, cost"]
    end
    W1 -.-> OTEL
    W2 -.-> OTEL
    OTEL -.-> LF
    OTEL -.-> MET
```

### 10b. Phase 2: kinds and flags at scale

Phase 1 keeps kinds and flags as code (closed `Literal` lists) and flags in `kb_config.toml`: right for 4 pages and one engineer, because every new value comes with its handling. With a growing KB, SOPs and content owners outside engineering, three things change:

1. **Flag → action policy table.** Flags move to a Postgres table, each mapped to a small, fixed set of **actions** the workflow already implements: `exclude_from_answers` · `facts_only` · `route_to(<destination>)` · `warn_agent(<reason_code>)` · `answer_only_chunk_text`. Content teams can add a flag (e.g. `outdated`, `region_specific`) and pick its actions without a deploy. A **new action** still needs code + tests; a flag with no action is rejected.
2. **Flags suggested automatically, confirmed by a human.** Ingestion proposes flags and a reviewer approves them in the KB explorer:
   - `conflict`: two chunks state different numbers/times for the same thing (e.g. "instant" vs "60 minutes").
   - `defective`: a chunk on page A is near-identical to a chunk on page B but talks about the other topic (the withdraw/deposit copy-paste).
   - `gap_link`: a link or "click here" pointing to a page not in the KB.
   - `promo` / `compliance`: by page section or source type.
   Every flag keeps who set it, when and why (replaces `KB_FINDINGS.md`), and the audit log records which flags affected each reply.
3. **Kinds from a generic parser.** New source types (SOP PDFs, tables, internal wiki) get a parser per source type with a heading-aware fallback, and chunk IDs are generated from page + heading (config only for overrides), so a new FAQ is indexed straight away and shown in a review list instead of failing ingestion.

Safety stays the same: unknown flags and unconfirmed `defective`/`compliance` suggestions are treated conservatively (excluded from answers until reviewed).

**Parser plan.** `kb/parse.py` is written for these 4 pages: it finds parts by the site's CSS class names. Phase 1 safety net: `tests/test_kb_ingest.py` expects exactly the planned 26 chunks, so a site change fails loudly instead of shipping an empty or partial KB. Limits and fixes, most important first:

| Weakness today | What goes wrong | Phase 2 fix |
|---|---|---|
| Depends on CSS class names (`accordion__item`, `seps-list__content-wrap`) | a redesign renames them → parser finds nothing | parse by document **structure** (h1–h4, lists, paragraphs); class rules kept only as a fast path |
| Unknown sections are skipped silently | a new section (e.g. a fee table) never reaches the KB, nobody notices | **coverage check**: compare the page's visible text with the text in its chunks; warn above a threshold (e.g. > 10% missing) |
| HTML only | SOPs arrive as PDF / Word / wiki | one parser per source type behind the same `parse() → blocks` interface |
| Tables and links flattened | a fee table becomes a run-on sentence; "click here" loses its target | tables kept as labelled rows ("Account: Standard · Min deposit: 8 USD"); link URLs kept (also feeds automatic `gap_link`) |
| Steps always one block | a 30-step SOP becomes one huge chunk with a blurry embedding | split long sections by size, repeating the heading on each piece |
| English pages only | the help site exists in 13 languages | parse each language version, store the language per chunk |

`Source` generalises at the same time: `title`, `location` (URL or document id), `source_type` (web_page / pdf / sop …), optional `locator` ("page 4", "§2.1") and `public`: internal SOPs are cited **to the agent only**, never in the customer reply.

---

## 11. Phase 2: ticket lifecycle with the helpdesk

```mermaid
stateDiagram-v2
    [*] --> Received: webhook
    Received --> Duplicate: ticket ID seen
    Duplicate --> [*]
    Received --> Queued: enqueue
    Queued --> Processing: worker picks up
    Processing --> Queued: transient error, retry with backoff
    Processing --> DeadLetter: failed 3 times
    DeadLetter --> ManualHandling: tag in helpdesk
    Processing --> DraftReady: auto_draft_ok
    Processing --> HumanRequired: any trigger
    DraftReady --> NoteWritten: private note posted
    HumanRequired --> NoteWritten: note + group assigned
    NoteWritten --> SentByAgent: agent reviews, edits, sends
    ManualHandling --> SentByAgent
    SentByAgent --> Audited: final text stored, draft vs final diff
    Audited --> [*]
```

---

## 12. Phase 1 → Phase 2: what changes

| Concern | Phase 1 (`v1-ship`) | Phase 2 (`v2-scale`) |
|---|---|---|
| Ticket intake | JSONL file / CLI / web form | Helpdesk webhook → ingest API |
| Processing | direct, in-process | Redis queue + workers, retries, DLQ |
| Idempotency | not needed | ticket-ID keys in Postgres |
| Return path | results.jsonl / web page | outbox → helpdesk REST API (private note) |
| KB in prompt | top-k from vector search (D1) | top-k from hybrid retrieval + reranker |
| KB updates | `copilot kb index`: incremental (per-chunk hash), only writer (D8) | same job, triggered on KB/SOP change (CI or scheduler) + conflict report |
| Vector store | pgvector (Docker) or in-memory (tests) | pgvector + Postgres full-text for hybrid search |
| Audit log | append-only JSONL | insert-only Postgres / write-once storage |
| Tracing | Langfuse optional (cloud, synthetic data) | Langfuse self-hosted via OpenTelemetry |
| Cost control | prompt caching | + answer cache, model split per step |
| **Unchanged** | the LangGraph co-pilot core, prompts, rules, eval set | same |

---

## 14. Code layout

Layers: **entry points** (CLI, web server) are thin and call **services**; services never import from entry points. The CLI, the API, the eval runner and the LangGraph workflow all reuse the same services.

```
src/copilot/
├── main.py                 starts the FastAPI server (serve); the CLI entry is cli.py:app
├── cli.py                  CLI for one-off jobs: kb, search, run, eval (does NOT start the server)
├── config.py               settings: models, thresholds, categories, language tiers (env + toml)
│
├── server/                 ── ENTRY POINT: the FastAPI web server (thin) ──
│   ├── app.py              create_app(): builds the app (KB, retriever, routes); main.py runs it
│   ├── deps.py             shared dependencies (KB, retriever, workflow)
│   ├── middleware.py       request ID + one log line per request    ✅
│   └── routes/
│       ├── health.py       GET /health                         ✅
│       ├── kb.py           GET /kb/chunks, /kb/chunks/{id}      ✅
│       ├── search.py       POST /search  (playground)
│       ├── tickets.py      POST /tickets, GET /tickets/{id}, POST /tickets/{id}/resume (HITL)
│       ├── webhook.py      POST /webhook/helpdesk  (Phase 2)
│       └── pages.py        HTML pages: ticket view, KB explorer (FR-9)
│
├── db.py                   one Postgres connection pool per process (shared by store + index)  ✅
├── services.py             KB reader + retriever, READ-ONLY, no KB snapshot (D8, D9)   ✅
│
├── kb/                     ── RAG: INGESTION (offline) ──
│   ├── models.py           Chunk, Source, KnowledgeBase, content hashes, diff  ✅
│   ├── parse.py            HTML → blocks                       ✅
│   ├── ingest.py           blocks → chunks, flags, hash        ✅
│   └── store.py            KBStore: chunks in Postgres (written by kb index, read by the server)  ✅
│
├── retrieval/              ── RAG: RETRIEVAL (online) ──
│   ├── embeddings.py       multilingual embedding model (bge-m3 / multilingual-e5)
│   ├── index.py            VectorIndex interface: PgVectorIndex (app, eval, Phase 2) + InMemoryIndex (tests) (D7)
│   ├── indexer.py          index_kb(): the ONLY writer; embeds new/changed chunks only (D8)  ✅
│   ├── search.py           Retriever (read-only): query → ranked chunks; best score = abstain signal
│   ├── bm25.py             keyword search (Phase 2 hybrid)
│   └── rerank.py           cross-encoder reranker (Phase 2)
│
├── llm/                    ── RAG: GENERATION (provider layer) ──
│   ├── client.py           provider-agnostic interface + Claude implementation
│   └── prompts/            classify.md, draft.md, judge.md (versioned)
│
├── guardrails/             ── DETERMINISTIC SAFETY ──
│   ├── pii.py              typed placeholders, Luhn, digit normalisation
│   ├── policy_gate.py      hard rules per intent
│   ├── post_check.py       citations, numbers-in-source, advice language
│   └── templates.py        pre-approved replies
│
├── workflow/               ── LANGGRAPH ORCHESTRATION ──
│   ├── state.py            CopilotState
│   ├── nodes.py            pii_mask, classify, safety_check, policy_gate, draft, merge, post_check, route
│   ├── routing.py          reason codes, destinations
│   └── graph.py            StateGraph wiring, checkpointer, interrupt (HITL)
│
├── audit/
│   └── log.py              append-only audit record (draft AND final)
│
└── eval/
    ├── retrieval.py        recall@k, precision@k, MRR, nDCG, abstain threshold sweep
    ├── workflow.py         end-to-end scoring on labelled + adversarial sets
    └── report.py           writes docs/BENCHMARK.md tables

tests/                      unit tests per module (no LLM calls)
eval/                       datasets (labelled, adversarial, retrieval queries)
knowledge_base/                         raw pages, config, chunk plan
```

**The RAG is three layers**, so each can be tested and swapped on its own:
1. **Ingestion** (`src/copilot/kb/` + `retrieval/indexer.py`, data in `knowledge_base/`): pages → chunks → vectors in Postgres. An offline job (`copilot kb index`), incremental, the only writer (D8).
2. **Retrieval** (`retrieval/`): query → ranked chunks + scores. Measured with recall/precision/MRR on `eval/retrieval_queries.jsonl`.
3. **Generation** (`llm/` + the `draft` node in `workflow/`): chunks + ticket → grounded draft with citations. Measured with the end-to-end eval.

## 13. Key design choices

Full reasoning in [DECISIONS.md](DECISIONS.md).

| Choice | Why |
|---|---|
| Helpdesk adapter in front of the core | Don't build a helpdesk; plug into the one they use. Phase 1 = JSONL mock. |
| LangGraph as a workflow, two LLM calls, gate between (D3) | Hard rules enforced by code before any drafting; cheaper routed tickets; testable steps. |
| Top-k retrieval only (D1, revised) | One path for Phase 1 and 2; tighter citations; the retrieval eval is the quality gate; whole-KB kept only as benchmark baseline C0. |
| Model chosen by benchmark (D2) | Cheapest configuration with 0 violations; model cost is small vs agent time. |
| Layered confidence (D4) | Rules + self-report + LLM judge; no single signal trusted alone. |
| CLI + small web app (D5) | Runnable in one command; visual walkthrough. |
| Own audit log + Langfuse (D6) | Audit is the legal record (draft and final); tracing is for debugging. |
| pgvector, not a separate vector DB (D7) | Postgres is needed anyway in Phase 2; one database to run, secure and back up; right size for the scale |
| Simple queue, not Kafka | 1–2 tickets/min at peak; need retries and spikes, not throughput. |
| Fail-open | If the co-pilot is down, agents work as before. |

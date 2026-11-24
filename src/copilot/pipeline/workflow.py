"""The ticket workflow (D3): a LangGraph STATE MACHINE, not autonomous agents.

    pattern_check ─┬─ spam / empty: no model, no LLM ───────────────────────────────► route
      (exact       ├─ injection phrase (certain) ───────┐                              ▲
       phrases,    │                                    ▼                              │
       spam)       └─► injection_check ─► risk_classifier ─┬─ injection: NO LLM, ───────┤
                      (model: catches      (bge-m3 + LR,    │  no AI draft               │
                       what patterns miss) 6 risks, D12)    │                            │
                                                            └─► classify_safety ─┐       │
                                                            └─► classify_triage ─┤       │
                                                                                 ▼       │
                                  validate_classification ─► confidence_gate ─► policy_gate
                                  (merge both, fix toward    (self-report AND     │
                                   the stricter value)        rule signals)       │
             ┌─ all blocked ─► template_reply ──────────────────────────────► compose ┤
    policy_gate                                                                ▲       │
             └─► retrieve ─► draft ─► post_check ──────────────────────────────┘       │
                                ▲          │ (one redraft)                             ▼
                                └──────────┘                                route ─► audit ─► END

Why a fixed graph: the hard rules are enforced by code at fixed points (policy_gate BEFORE any
reply is written, post_check AFTER), so no model decides whether a rule applies.

PII is masked BEFORE the graph runs (tickets/service.py `intake`), so the graph state, LangSmith
traces and the LLM only ever see placeholders; the placeholder → value mapping never enters
the state. The background worker runs this graph (tickets/worker.py).

Implemented: every node except audit.
The other nodes are marked @placeholder: they produce nothing and monitoring reports them as
placeholders, never as work done.
Dependencies (the risk classifier, later the LLM clients) come in through LangGraph's runtime
context (`Deps`), so nodes are testable with fakes.
View it: `uv run langgraph dev` (LangGraph Studio) or `uv run copilot graph` (Mermaid diagram).
"""

import operator
import time
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Annotated, Any, Literal, TypedDict

import anthropic
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from copilot import telemetry as tel
from copilot.log import get_logger
from copilot.pipeline import grounding, prompts, templates, validation
from copilot.pipeline import injection as injection_gate
from copilot.pipeline import patterns as pattern_rules
from copilot.pipeline import policy as policy_rules
from copilot.pipeline import post_check as post_checks
from copilot.pipeline import routing as routing_rules
from copilot.pipeline.llm import LLMOutputError, structured
from copilot.pipeline.risk_classifier import load_injection_rules
from copilot.pipeline.schemas import Classification, DraftOut, Routing, SafetyOut, TriageOut
from copilot.pipeline.validation import script_of

log = get_logger(__name__)

MAX_DRAFT_ATTEMPTS = 2
_INJECTION_RULES = (
    load_injection_rules()
)  # classifier/thresholds.toml [injection]  # first draft + one redraft after a failed post_check


# ---- State ----------------------------------------------------------------------------------


class InputState(TypedDict):
    """What the graph accepts (and what Studio shows as the input form). Already PII-masked."""

    ticket_id: str
    subject: str
    body: str


class OutputState(TypedDict, total=False):
    """What the graph returns to the caller."""

    reply: str
    citations: list[dict]
    routing: Routing
    steps: list[str]


class State(InputState, OutputState, total=False):
    """Everything the nodes share. Each node returns only the keys it changes."""

    patterns: dict  # pattern_check: {"hits": [{kind, pattern}], "spam_reason": str | None}
    injection: dict  # injection_check: {"detected", "evidence": [...], "model": {probability...}}
    safety: dict  # classify_safety: SafetyOut + checks (unquoted intents)
    triage: dict  # classify_triage: TriageOut (validated)
    classification_fixes: list[
        str
    ]  # validate_classification: corrections toward the stricter value
    confidence: dict  # confidence_gate: self-reported, threshold, signals, passed
    llm_usage: Annotated[list[dict], operator.add]  # one Usage per LLM call (tokens, cost)
    risk: dict  # risk_classifier: signals, probabilities, category (+ probability, margin)
    classification: Classification | None
    policy: dict  # policy_gate: per-intent result, ticket rule, evidence, compliance
    blocked_intents: list[int]  # indexes into classification.intents
    template_parts: list[str]  # template keys for the blocked intents, in reply order
    template_texts: list[dict]  # template_reply: {key, text, translated}
    notes_for_agent: list[str]  # internal notes shown next to the draft, never sent
    retrieved: list[tuple[str, float]]  # (chunk_id, score)
    sources: list[dict]  # retrieve: the chunks given to the drafter (id, section, text, flags)
    draft: DraftOut | None
    draft_attempts: int
    post_check_failures: list[str]
    # routing reason codes collected along the way (reducer: each node appends its own)
    reasons: Annotated[list[str], operator.add]
    # `steps` uses a reducer: each node APPENDS its name, so the path through the graph is visible
    steps: Annotated[list[str], operator.add]


@dataclass
class Deps:
    """What nodes need from outside (passed as LangGraph runtime context)."""

    risk_classifier: Any = None  # pipeline.risk_classifier.RiskClassifier (or a test fake)
    injection_guard: Any = None  # pipeline.injection_guard.InjectionGuard (or a test fake)
    classify_llm: Any = None  # ChatAnthropic for the classification calls (or a test fake)
    confidence_threshold: float = 0.7  # PRD FR-5.1 low_confidence (CONFIDENCE_THRESHOLD)
    low_resource_languages: tuple[str, ...] = ("ckb", "sw")  # LOW_RESOURCE_LANGUAGES
    retriever: Any = None  # retrieval.search.Retriever over the served KB (or a test fake)
    draft_llm: Any = None  # ChatAnthropic for the draft call (LLM_DRAFT_MODEL)
    top_k: int = 5  # RETRIEVAL_TOP_K
    weak_retrieval_threshold: float = 0.44  # WEAK_RETRIEVAL_THRESHOLD (per embedding model)


def build_deps(st, risk, guard, retriever) -> Deps:
    """Deps from settings + already-loaded services (the worker: load_services)."""
    from copilot.pipeline.llm import chat_model  # noqa: PLC0415

    return Deps(
        risk,
        guard,
        chat_model(st.llm_classify_model, st.anthropic_api_key, st.llm_timeout_s),
        st.confidence_threshold,
        tuple(st.low_resource_languages),
        retriever,
        chat_model(st.llm_draft_model, st.anthropic_api_key, st.llm_timeout_s, max_tokens=2048),
        st.retrieval_top_k,
        st.weak_retrieval_threshold,
    )


@lru_cache
def default_deps() -> Deps:
    """For callers that pass no context (LangGraph Studio, scripts): build from .env, with an
    in-memory index of the KB. Loads bge-m3 (~10 s) once. The worker passes its own Deps."""
    from copilot.config import get_settings  # noqa: PLC0415
    from copilot.kb.ingest import build_kb  # noqa: PLC0415
    from copilot.pipeline.injection_guard import InjectionGuard  # noqa: PLC0415
    from copilot.pipeline.risk_classifier import (  # noqa: PLC0415
        RiskClassifier,
        load_injection_rules,
    )
    from copilot.retrieval.embeddings import get_embedder  # noqa: PLC0415
    from copilot.retrieval.index import InMemoryIndex  # noqa: PLC0415
    from copilot.retrieval.indexer import index_kb  # noqa: PLC0415
    from copilot.retrieval.search import Retriever  # noqa: PLC0415

    st, rules = get_settings(), load_injection_rules()
    embedder, index = get_embedder(st.embedding_model), InMemoryIndex()
    index_kb(build_kb(), embedder, index)
    guard = InjectionGuard(
        st.injection_guard_model,
        {"weak": rules["guard_weak"], "strong": rules["guard_strong"]},
        st.huggingface_api_key.get_secret_value() if st.huggingface_api_key else None,
    )
    risk = RiskClassifier(embedder, model_name=st.embedding_model)
    return build_deps(st, risk, guard, Retriever(embedder, index))


_REQUIRED = ("risk_classifier", "injection_guard", "classify_llm", "retriever", "draft_llm")


def _deps(runtime: Runtime | None) -> Deps:
    """The caller's Deps; any missing service is taken from default_deps (loaded only then)."""
    ctx = getattr(runtime, "context", None)
    if not isinstance(ctx, Deps):
        return default_deps()
    missing = [f for f in _REQUIRED if getattr(ctx, f) is None]
    if not missing:
        return ctx
    d = default_deps()
    return replace(ctx, **{f: getattr(d, f) for f in missing})


def _text(state: State) -> str:
    return f"{state.get('subject', '')}\n{state.get('body', '')}".strip()


# ---- Implemented nodes ----------------------------------------------------------------------


def pattern_check(state: State) -> dict:
    """Exact phrases (prompt injection, account security) and spam/empty, BEFORE any model or
    LLM. Evidence = pattern names, never the customer's text."""
    r = pattern_rules.check(_text(state))
    hits = [{"kind": h.kind, "pattern": h.pattern} for h in r.hits]
    return {"steps": ["pattern_check"], "patterns": {"hits": hits, "spam_reason": r.spam_reason}}


def injection_check(state: State, runtime: Runtime[Deps]) -> dict:
    """The injection gate (pipeline/injection.py), BEFORE any LLM: the guard model (NeuralTrust)
    + our bge-m3 head as a backup. Any layer → no LLM call, no AI draft, a person replies."""
    deps, text = _deps(runtime), _text(state)
    hits = (state.get("patterns") or {}).get("hits", [])
    gate = injection_gate.check(
        hits,
        head=deps.risk_classifier.injection(text),
        guard=deps.injection_guard.score(text),
        head_threshold=_INJECTION_RULES["head_detect"],
    )
    return {"steps": ["injection_check"], "injection": gate}


def risk_classifier(state: State, runtime: Runtime[Deps]) -> dict:
    """Probability per customer risk (and a category guess) from bge-m3 + logistic regression.
    Runs on injection tickets too: free and local, it tells the agent what else is in there."""
    r = _deps(runtime).risk_classifier.predict(_text(state))
    return {
        "steps": ["risk_classifier"],
        "risk": {
            "signals": [asdict(sig) for sig in r.signals],
            "probabilities": r.probabilities,
            "category": r.category,
            "category_probability": r.category_probability,
            "category_margin": r.category_margin,
        },
    }


def injection_reply(state: State) -> dict:
    """Injection path (no LLM ever reads this ticket). If the LOCAL classifier is strongly sure
    the ticket also asks for trading advice, add the fixed decline (the brief: detect advice
    requests and decline politely), in the ticket's script (Arabic script → ar, else en).
    Everything else in the ticket is left to the person (senior support)."""
    risk = state.get("risk") or {}
    strong = {s["kind"] for s in risk.get("signals", []) if s["strength"] == "strong"}
    if "trading_advice" not in strong:
        return {"steps": ["injection_reply"]}
    lang = "ar" if script_of(_text(state)) == "arabic" else "en"
    text, _ = templates.render("decline_advice", lang)
    return {
        "steps": ["injection_reply"],
        "reply": text,
        "reasons": ["hard_rule_topic"],
        "notes_for_agent": ["Prompt injection: no AI read this ticket. Only the fixed "
                            "trading-advice decline was added; answer the rest yourself."],
    }  # fmt: skip


# ---- Nodes (placeholders) -------------------------------------------------------------------


def placeholder(fn):
    """Marks a node that is not implemented yet. Monitoring reports it as a placeholder (amber
    in Grafana, listed in the ticket result) instead of as real work done. Remove the decorator
    when the node is implemented."""
    fn.implemented = False
    return fn


MAX_INTENTS = 5


def classify_safety(state: State, runtime: Runtime[Deps]) -> dict:
    """LLM call 1a (recall first): which parts of the ticket touch a hard rule: intents with
    topic + decision_requested, vulnerable, security, and a 2nd line against injection.
    Never reached when the injection gate fired. Validated in 4 layers: structured output,
    Pydantic, business rules below, and fail closed (an error is never "safe")."""
    r = structured(
        _deps(runtime).classify_llm,
        SafetyOut,
        prompts.SAFETY_SYSTEM,
        prompts.safety_user(state.get("subject", ""), state.get("body", "")),
        name="classify_safety",
    )
    out: SafetyOut = r.parsed
    if not 1 <= len(out.intents) <= MAX_INTENTS:
        raise LLMOutputError(f"classify_safety: {len(out.intents)} intents (expected 1-5)")
    ticket = _norm(_text(state))
    unquoted = [i for i, it in enumerate(out.intents) if _norm(it.text) not in ticket]
    return {
        "steps": ["classify_safety"],
        "safety": {
            **out.model_dump(),
            "unquoted_intents": unquoted,  # quotes not found in the ticket: shown to the agent
            "prompt_version": prompts.PROMPT_VERSION,
            "model": r.model,
        },
        "llm_usage": [{"node": "classify_safety", **r.usage.model_dump()}],
    }


def _norm(s: str) -> str:
    """Lowercase, one space, no edge punctuation: for checking a quote is in the ticket."""
    return " ".join(s.lower().split()).strip(" .,!?;:'\"")


def classify_triage(state: State, runtime: Runtime[Deps]) -> dict:
    """LLM call 1b, in parallel with safety: category, urgency, language, reply_language,
    sentiment, needs_account_action, search_query, confidence. Validated: structured output,
    Pydantic, then validation.check_triage (bad language code, NaN confidence → retry)."""
    r = structured(
        _deps(runtime).classify_llm,
        TriageOut,
        prompts.TRIAGE_SYSTEM,
        prompts.triage_user(state.get("subject", ""), state.get("body", "")),
        name="classify_triage",
    )
    out = validation.check_triage(r.parsed)
    return {
        "steps": ["classify_triage"],
        "triage": {
            **out.model_dump(),
            "prompt_version": prompts.TRIAGE_PROMPT_VERSION,
            "model": r.model,
        },
        "llm_usage": [{"node": "classify_triage", **r.usage.model_dump()}],
    }


def validate_classification(state: State) -> dict:
    """Both answers → ONE Classification (validation.merge). Contradictions are resolved toward
    the stricter value (decision → topic, strictest category, vulnerable → critical) and every
    correction is kept: the agent sees it and confidence_gate counts it as a signal."""
    clean = {k: v for k, v in (state.get("safety") or {}).items() if k in SafetyOut.model_fields}
    tri = {k: v for k, v in (state.get("triage") or {}).items() if k in TriageOut.model_fields}
    c, fixes = validation.merge(clean, tri)
    return {"steps": ["validate_classification"], "classification": c,
            "classification_fixes": fixes}  # fmt: skip


def confidence_gate(state: State, runtime: Runtime[Deps]) -> dict:
    """Self-reported confidence is never trusted alone: rule signals (the two LLMs disagree,
    the local classifier is sure of something else, quotes not in the ticket, language code vs
    script) back it up. Any signal → low_confidence. Also low_resource_language. Routing only:
    the draft is still written, a person reviews it with the reasons shown."""
    deps = _deps(runtime)
    r = validation.confidence(
        state["classification"],
        text=_text(state),
        risk=state.get("risk"),
        unquoted_intents=(state.get("safety") or {}).get("unquoted_intents", []),
        fixes=state.get("classification_fixes", []),
        threshold=deps.confidence_threshold,
        low_resource_languages=list(deps.low_resource_languages),
    )
    return {
        "steps": ["confidence_gate"],
        "confidence": {
            "self_reported": r.self_reported,
            "threshold": r.threshold,
            "signals": r.signals,
            "passed": r.passed,
        },  # fmt: skip
        "reasons": r.reasons,
    }


def policy_gate(state: State) -> dict:
    """The hard rules, in code (pipeline/policy.py): per intent, blocked or allowed; vulnerable
    or security-sensitive blocks the whole ticket. Strictest of LLM, classifier and patterns."""
    r = policy_rules.gate(
        state["classification"], state.get("risk"), (state.get("patterns") or {}).get("hits", [])
    )
    vulnerable = r.ticket_rule == "vulnerable"
    notes = ["Vulnerable customer: reply personally; no product talk."] if vulnerable else []
    return {
        "steps": ["policy_gate"],
        "policy": {
            "intents": [vars(g) for g in r.intents],
            "ticket_rule": r.ticket_rule,
            "evidence": r.evidence,
            "compliance": r.compliance,
        },
        "blocked_intents": r.blocked,
        "template_parts": r.templates,
        "reasons": r.reasons,
        "notes_for_agent": notes,
    }


def template_reply(state: State, runtime: Runtime[Deps]) -> dict:
    """Fixed texts for the blocked intents (pipeline/templates.py), in the reply language.
    No LLM writes them. A language without approved text, or a low-resource one, gets English +
    a note for the agent."""
    lang = state["classification"].reply_language
    if lang in _deps(runtime).low_resource_languages:
        lang = "en"
    texts, notes = [], list(state.get("notes_for_agent", []))
    for key in state.get("template_parts", []):
        text, translated = templates.render(key, lang)
        texts.append({"key": key, "text": text, "translated": translated})
        if not translated:
            notes.append(f"Template '{key}' has no {lang} text: English used, translate.")
        elif "machine translation" in templates.review_status(key, lang):
            notes.append(f"Template '{key}' in {lang} is a machine translation pending native "
                         "review: check the wording.")  # fmt: skip
    return {"steps": ["template_reply"], "template_texts": texts, "notes_for_agent": notes}


def retrieve(state: State, runtime: Runtime[Deps]) -> dict:
    """Search the KB for the ANSWERABLE parts only (pipeline/grounding.py): triage's English
    search query + each allowed intent's quote, best score per chunk, top k. Defective, promo and
    compliance chunks are never returned. Best score below the threshold → weak_retrieval (no
    draft: a person answers; threshold from `copilot eval retrieval`)."""
    deps = _deps(runtime)
    qs = grounding.queries(state["classification"], state.get("blocked_intents", []))
    hits = grounding.merge(
        [deps.retriever.search(q, deps.top_k, grounding.EXCLUDED_FLAGS) for q in qs], deps.top_k
    )
    best = hits[0].score if hits else 0.0
    weak = bool(qs) and best < deps.weak_retrieval_threshold
    return {
        "steps": ["retrieve"],
        "retrieved": [(h.chunk_id, round(h.score, 4)) for h in hits],
        "sources": [] if weak else [grounding.source(h) for h in hits],
        "reasons": ["weak_retrieval"] if weak else [],
    }


def draft(state: State, runtime: Runtime[Deps]) -> dict:
    """LLM call 2: reply to the ALLOWED intents only, from the retrieved chunks only; cites chunk
    IDs, or abstains / answers partially. Validated: structured output, Pydantic, then
    grounding.check_draft (unknown citation, empty answer, answer without citation → retry)."""
    c: Classification = state["classification"]
    blocked = set(state.get("blocked_intents", []))
    allowed = [it for i, it in enumerate(c.intents) if i not in blocked]
    sources = state.get("sources", [])
    questions = [f'"{it.text}" ({it.topic})' for it in allowed if it.text.strip()]
    fails = state.get("post_check_failures", [])  # set → this is the redraft
    has_question = bool(c.search_query.strip())
    deps = _deps(runtime)
    # PRD FR-5.1: low-resource language → an ENGLISH draft for the agent, not a weak translation
    low_resource = c.reply_language in deps.low_resource_languages
    lang = "en" if low_resource else c.reply_language
    r = structured(
        deps.draft_llm,
        DraftOut,
        prompts.DRAFT_SYSTEM,
        prompts.draft_user(
            lang,
            questions if has_question else [],
            len(blocked),
            sources,
            state.get("subject", ""),
            state.get("body", ""),
            feedback=post_checks.feedback(fails) if fails else "",
        ),
        name="draft",
    )
    d = grounding.check_draft(r.parsed, {s["id"] for s in sources}, has_question)
    cited = {s["id"]: s for s in sources if s["id"] in d.cited_chunk_ids}
    reasons, notes = [], list(state.get("notes_for_agent", []))
    # partial: answered + the gap in a note (FR-3.2b); routed only when the gap is sensitive
    if d.abstain or (d.partial and d.gap_needs_person):
        reasons.append("not_in_kb")
    if low_resource:
        notes.append(f"Draft is in English ({c.reply_language} is a low-resource language): "
                     "translate, or have a native speaker reply.")  # fmt: skip
    if any("conflict" in s["flags"] for s in cited.values()):
        reasons.append("kb_conflict")
    if any("route" in s["flags"] for s in cited.values()):
        reasons.append("account_action_required")  # the KB itself says: contact support
    if d.note_for_agent.strip():
        notes.append(f"Drafter: {d.note_for_agent.strip()}")
    if d.uncovered:
        notes.append("Not covered by the help pages: " + "; ".join(d.uncovered))
    return {
        "steps": ["draft"],
        "draft": d,
        "draft_attempts": state.get("draft_attempts", 0) + 1,
        "reasons": reasons,
        "notes_for_agent": notes,
        "llm_usage": [{"node": "draft", **r.usage.model_dump()}],
    }


def post_check(state: State, runtime: Runtime[Deps]) -> dict:
    """Deterministic checks on the draft (pipeline/post_check.py): numbers only from cited
    chunks, no advice wording, no promises or account claims, known citations and placeholders,
    the right script. A failure → one redraft with the failures as feedback; a second failure →
    post_check_failed, and the draft is withheld (a person writes the reply)."""
    d = state.get("draft")
    c = state["classification"]
    # the language the draft was WRITTEN in: English for a low-resource language (FR-5.1)
    lang = "en" if c.reply_language in _deps(runtime).low_resource_languages else c.reply_language
    fails = post_checks.check(d, state.get("sources", []), _text(state), lang)
    out: dict = {"steps": ["post_check"], "post_check_failures": fails}
    if fails and state.get("draft_attempts", 0) >= MAX_DRAFT_ATTEMPTS:
        out["reasons"] = ["post_check_failed"]
        notes = list(state.get("notes_for_agent", []))
        notes.append(f"AI draft withheld: failed checks twice ({'; '.join(fails)}).")
        out["notes_for_agent"] = notes
    return out


def compose(state: State) -> dict:
    """One reply in intent order (PRD FR-4.6): each blocked intent's template where it first
    appears, the grounded draft where the first allowed intent is. An allowed part with no
    usable draft (abstained, weak retrieval) is left out and the agent is told to add it; with
    nothing at all to show, there is NO reply (null) and a person writes it.
    Citations: one per (cited chunk, page), for the agent."""
    c = state.get("classification")
    texts = {t["key"]: t["text"] for t in state.get("template_texts", [])}
    gates = (state.get("policy") or {}).get("intents", [])
    draft = state.get("draft")
    usable = (
        draft is not None
        and not draft.abstain
        and bool(draft.reply.strip())
        and not state.get("post_check_failures")
    )  # a draft that failed is never shown
    if c is None:
        return {"steps": ["compose"]}
    parts, used, missing = [], set(), []
    for g in gates:
        if g["blocked"]:
            tag, piece = g["template"], texts.get(g["template"])
        elif usable:
            tag, piece = "draft", draft.reply.strip()
        else:
            missing.append(c.intents[g["index"]].text)
            continue
        if piece and tag not in used:
            used.add(tag)
            parts.append(piece)
    out: dict = {"steps": ["compose"], "reply": "\n\n".join(parts) if parts else None}
    if usable:
        out["citations"] = grounding.citations(draft.cited_chunk_ids, state.get("sources", []))
    if missing and parts:
        notes = list(state.get("notes_for_agent", []))
        notes.append(
            "Draft is INCOMPLETE: add an answer to " + "; ".join(f'"{m}"' for m in missing)
        )
        out["notes_for_agent"] = notes
    return out


def route(state: State) -> dict:
    """Reason codes → auto_draft_ok or human_required + destination (pipeline/routing.py)."""
    return {"steps": ["route"], "routing": routing_rules.decide(state)}


@placeholder
def audit(state: State) -> dict:
    # TODO step 7: append one masked record to the audit log (the legal record, not LangSmith)
    return {"steps": ["audit"]}


# ---- Edges ----------------------------------------------------------------------------------


LLM_CLASSIFIERS = ["classify_safety", "classify_triage"]


def after_patterns(state: State) -> Literal["route", "risk_classifier", "injection_check"]:
    """Spam / empty → straight to routing (a person, low priority): no model, no LLM, no cost.
    An exact injection phrase is certain → skip the gate, run only the local risk classifier
    (so the agent sees what else was asked) and route. Everything else → the injection gate,
    which catches what the patterns missed."""
    if (state.get("patterns") or {}).get("spam_reason"):
        return "route"
    if routing_rules.injection_evidence(state):
        return "risk_classifier"
    return "injection_check"


def after_risk(state: State) -> list[str] | Literal["injection_reply"]:
    """Injection (exact pattern or the gate's model) → injection_reply → routing: the attacker's
    text never reaches an LLM. Clean → the two LLM classification calls in parallel."""
    if routing_rules.injection_evidence(state):
        return "injection_reply"
    return LLM_CLASSIFIERS


def after_gate(state: State) -> Literal["route", "retrieve", "template_reply"]:
    """Any part blocked → its fixed template first (also on a mixed ticket, and on an injection
    the safety LLM caught: the decline still goes out, no draft). Injection with nothing
    blocked → a person, no draft. Else → grounded draft."""
    injection = (state.get("safety") or {}).get("injection_suspected")
    if state.get("template_parts"):
        return "template_reply"  # fixed texts only (no LLM): also on an injection (eval X04)
    return "route" if injection else "retrieve"


def after_templates(state: State) -> Literal["retrieve", "compose"]:
    """All intents blocked → only the templates. A mixed ticket → draft the allowed part too."""
    if (state.get("safety") or {}).get("injection_suspected"):
        return "compose"  # an injection never reaches retrieve/draft
    c = state.get("classification")
    n_intents = len(c.intents) if c else 1
    if len(state.get("blocked_intents", [])) >= n_intents:
        return "compose"
    return "retrieve"


def after_retrieve(state: State) -> Literal["draft", "compose"]:
    """Weak retrieval → no draft call (a person answers; reason weak_retrieval). A ticket with no
    question (a thank-you) still gets a short polite draft (PRD FR-2.1c)."""
    if "weak_retrieval" in state.get("reasons", []):
        return "compose"
    return "draft"


def after_post_check(state: State) -> Literal["draft", "compose"]:
    """A failed check gets ONE redraft; if it fails again, route() sends it to a human."""
    if state.get("post_check_failures") and state.get("draft_attempts", 0) < MAX_DRAFT_ATTEMPTS:
        return "draft"
    return "compose"


def _observed(name: str, fn):
    """Each node gets an OpenTelemetry span (Grafana/Tempo) and a duration metric, labelled
    with whether the node is implemented. LangSmith traces the same nodes on its own."""
    implemented = getattr(fn, "implemented", True)
    wants_runtime = "runtime" in fn.__code__.co_varnames[: fn.__code__.co_argcount]

    def run(state: State, runtime: Runtime[Deps]) -> dict:
        t0 = time.perf_counter()
        with tel.tracer.start_as_current_span(
            f"node.{name}", attributes={"node.implemented": implemented}
        ):
            out = fn(state, runtime) if wants_runtime else fn(state)
        tel.NODE_MS.record(
            (time.perf_counter() - t0) * 1000,
            {"node": name, "implemented": str(implemented).lower()},
        )
        return out

    run.__name__ = name
    return run


NODES = [
    ("pattern_check", pattern_check),
    ("injection_check", injection_check),
    ("risk_classifier", risk_classifier),
    ("injection_reply", injection_reply),
    ("classify_safety", classify_safety),
    ("classify_triage", classify_triage),
    ("validate_classification", validate_classification),
    ("confidence_gate", confidence_gate),
    ("policy_gate", policy_gate),
    ("template_reply", template_reply),
    ("retrieve", retrieve),
    ("draft", draft),
    ("post_check", post_check),
    ("compose", compose),
    ("route", route),
    ("audit", audit),
]
# node → implemented? (read by the worker so monitoring never reports a placeholder as real work)
NODE_IMPLEMENTED = {name: getattr(fn, "implemented", True) for name, fn in NODES}


# LLM nodes: one retry inside the graph on an invalid answer or a transient API error (PRD:
# "invalid JSON after one retry → technical_failure"). After that the node fails, the worker
# retries the ticket (resuming at this node) and, after 3 attempts, hands it to a person.
LLM_RETRY = RetryPolicy(
    max_attempts=2,
    initial_interval=1.0,
    retry_on=(
        LLMOutputError,
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
    ),
)
RETRY = {"classify_safety": LLM_RETRY, "classify_triage": LLM_RETRY, "draft": LLM_RETRY}


def build_graph(checkpointer=None):
    g = StateGraph(State, context_schema=Deps, input_schema=InputState, output_schema=OutputState)
    for name, fn in NODES:
        g.add_node(name, _observed(name, fn), retry_policy=RETRY.get(name))

    g.add_edge(START, "pattern_check")
    g.add_conditional_edges("pattern_check", after_patterns)
    g.add_edge("injection_check", "risk_classifier")
    g.add_conditional_edges("risk_classifier", after_risk, [*LLM_CLASSIFIERS, "injection_reply"])
    g.add_edge("injection_reply", "route")
    g.add_edge(LLM_CLASSIFIERS, "validate_classification")  # waits for both
    g.add_edge("validate_classification", "confidence_gate")
    g.add_edge("confidence_gate", "policy_gate")
    g.add_conditional_edges("policy_gate", after_gate)
    g.add_conditional_edges("retrieve", after_retrieve)
    g.add_edge("draft", "post_check")
    g.add_conditional_edges("post_check", after_post_check)
    g.add_conditional_edges("template_reply", after_templates)
    g.add_edge("compose", "route")
    g.add_edge("route", "audit")
    g.add_edge("audit", END)
    return g.compile(checkpointer=checkpointer)


# Module-level compiled graph: what LangGraph Studio loads (langgraph.json).
graph = build_graph()

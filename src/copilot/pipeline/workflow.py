"""The ticket workflow (D3): a LangGraph STATE MACHINE, not autonomous agents.

    pattern_check ─┬─ spam / empty: no model, no LLM ───────────────────────────────► route
      (exact       │                                                                   ▲
       phrases,    └─► injection_check ─► risk_classifier ─┬─ injection: NO LLM, ───────┤
       spam)          (patterns + model,  (bge-m3 + LR,    │  no AI draft               │
                       BEFORE any LLM)     6 risks, D12)   │                            │
                                                           └─► classify_safety ─┐       │
                                                           └─► classify_triage ─┴► policy_gate
                                                                                      │
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

Implemented: pattern_check, injection_check, risk_classifier, route. The other nodes are
marked @placeholder: they produce nothing and monitoring reports them as placeholders, never as
work done.
Dependencies (the risk classifier, later the LLM clients) come in through LangGraph's runtime
context (`Deps`), so nodes are testable with fakes.
View it: `uv run langgraph dev` (LangGraph Studio) or `uv run copilot graph` (Mermaid diagram).
"""

import operator
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from copilot import telemetry as tel
from copilot.log import get_logger
from copilot.pipeline import injection as injection_gate
from copilot.pipeline import patterns as pattern_rules
from copilot.pipeline import routing as routing_rules
from copilot.pipeline.schemas import Classification, DraftOut, Routing

log = get_logger(__name__)

MAX_DRAFT_ATTEMPTS = 2  # first draft + one redraft after a failed post_check


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
    risk: dict  # risk_classifier: signals, probabilities, category (+ probability, margin)
    classification: Classification | None
    blocked_intents: list[int]  # indexes into classification.intents
    template_parts: list[str]  # decline / neutral acknowledgement texts for blocked intents
    retrieved: list[tuple[str, float]]  # (chunk_id, score)
    draft: DraftOut | None
    draft_attempts: int
    post_check_failures: list[str]
    reasons: list[str]  # routing reason codes collected along the way
    # `steps` uses a reducer: each node APPENDS its name, so the path through the graph is visible
    steps: Annotated[list[str], operator.add]


@dataclass
class Deps:
    """What nodes need from outside (passed as LangGraph runtime context)."""

    risk_classifier: Any = None  # pipeline.risk_classifier.RiskClassifier (or a test fake)


@lru_cache
def default_deps() -> Deps:
    """For callers that pass no context (LangGraph Studio, scripts): build from .env.
    Loads bge-m3 (~10 s) once. The worker passes its own Deps instead."""
    from copilot.config import get_settings  # noqa: PLC0415
    from copilot.pipeline.risk_classifier import RiskClassifier  # noqa: PLC0415
    from copilot.retrieval.embeddings import get_embedder  # noqa: PLC0415

    st = get_settings()
    return Deps(RiskClassifier(get_embedder(st.embedding_model), model_name=st.embedding_model))


def _deps(runtime: Runtime | None) -> Deps:
    ctx = getattr(runtime, "context", None)
    return ctx if isinstance(ctx, Deps) and ctx.risk_classifier is not None else default_deps()


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
    """The injection gate (pipeline/injection.py), BEFORE any LLM: exact patterns + the
    injection model. Any signal → no LLM call, no AI draft, a person replies."""
    hits = (state.get("patterns") or {}).get("hits", [])
    model = _deps(runtime).risk_classifier.injection(_text(state))
    return {"steps": ["injection_check"], "injection": injection_gate.check(hits, model)}


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


# ---- Nodes (placeholders) -------------------------------------------------------------------


def placeholder(fn):
    """Marks a node that is not implemented yet. Monitoring reports it as a placeholder (amber
    in Grafana, listed in the ticket result) instead of as real work done. Remove the decorator
    when the node is implemented."""
    fn.implemented = False
    return fn


@placeholder
def classify_safety(state: State) -> dict:
    # TODO: LLM call 1 (safety, tuned for recall): intents + decision_requested, vulnerable,
    # security. Second line against injection: it also flags instructions aimed at the AI
    # (→ reason prompt_injection, no draft). Never reached when the injection gate fired.
    return {"steps": ["classify_safety"]}  # placeholder: produces nothing


@placeholder
def classify_triage(state: State) -> dict:
    # TODO: LLM call 2 (triage): category, urgency, sentiment, language, reply_language,
    # needs_account_action, confidence.
    return {"steps": ["classify_triage"]}  # placeholder: produces nothing


@placeholder
def policy_gate(state: State) -> dict:
    # TODO step 4: block intents that touch a hard rule (advice, complaint/KYC/AML decision,
    # vulnerable customer, security request). Code, not a prompt.
    return {"steps": ["policy_gate"], "blocked_intents": [], "template_parts": []}


@placeholder
def template_reply(state: State) -> dict:
    # TODO step 4: fixed decline / neutral acknowledgement (templates.py); no LLM writes it
    return {"steps": ["template_reply"]}


@placeholder
def retrieve(state: State) -> dict:
    # TODO step 7: top-k search with the existing Retriever (defective/promo chunks excluded)
    return {"steps": ["retrieve"], "retrieved": []}


@placeholder
def draft(state: State) -> dict:
    # TODO step 3: LLM call 2: answer only from the retrieved chunks, cite chunk IDs, or abstain
    return {
        "steps": ["draft"],
        "draft": None,
        "draft_attempts": state.get("draft_attempts", 0) + 1,
    }


@placeholder
def post_check(state: State) -> dict:
    # TODO step 5: numbers not in cited text, missing citations, advice wording, unknown chunk IDs
    return {"steps": ["post_check"], "post_check_failures": []}


@placeholder
def compose(state: State) -> dict:
    # TODO step 7: merge the grounded draft and any template parts; build page citations
    return {"steps": ["compose"]}  # placeholder: no reply produced (null), not an empty one


def route(state: State) -> dict:
    """Reason codes → auto_draft_ok or human_required + destination (pipeline/routing.py)."""
    return {"steps": ["route"], "routing": routing_rules.decide(state)}


@placeholder
def audit(state: State) -> dict:
    # TODO step 7: append one masked record to the audit log (the legal record, not LangSmith)
    return {"steps": ["audit"]}


# ---- Edges ----------------------------------------------------------------------------------


LLM_CLASSIFIERS = ["classify_safety", "classify_triage"]


def after_patterns(state: State) -> Literal["route", "injection_check"]:
    """Spam / empty → straight to routing (a person, low priority): no model, no LLM, no cost.
    Everything else → the injection gate."""
    if (state.get("patterns") or {}).get("spam_reason"):
        return "route"
    return "injection_check"


def after_risk(state: State) -> list[str] | Literal["route"]:
    """Injection (any layer) → routing: the attacker's text never reaches an LLM.
    Clean → the two LLM classification calls in parallel."""
    if (state.get("injection") or {}).get("detected"):
        return "route"
    return LLM_CLASSIFIERS


def after_gate(state: State) -> Literal["retrieve", "template_reply"]:
    """All intents blocked → only a template reply. Anything answerable → grounded draft."""
    c = state.get("classification")
    n_intents = len(c.intents) if c else 1
    if len(state.get("blocked_intents", [])) >= n_intents:
        return "template_reply"
    return "retrieve"


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
    ("classify_safety", classify_safety),
    ("classify_triage", classify_triage),
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


def build_graph():
    g = StateGraph(State, context_schema=Deps, input_schema=InputState, output_schema=OutputState)
    for name, fn in NODES:
        g.add_node(name, _observed(name, fn))

    g.add_edge(START, "pattern_check")
    g.add_conditional_edges("pattern_check", after_patterns)
    g.add_edge("injection_check", "risk_classifier")
    g.add_conditional_edges("risk_classifier", after_risk, [*LLM_CLASSIFIERS, "route"])
    g.add_edge(LLM_CLASSIFIERS, "policy_gate")  # waits for both
    g.add_conditional_edges("policy_gate", after_gate)
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "post_check")
    g.add_conditional_edges("post_check", after_post_check)
    g.add_edge("template_reply", "compose")
    g.add_edge("compose", "route")
    g.add_edge("route", "audit")
    g.add_edge("audit", END)
    return g.compile()


# Module-level compiled graph: what LangGraph Studio loads (langgraph.json).
graph = build_graph()

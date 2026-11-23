"""The ticket workflow (D3): a LangGraph STATE MACHINE, not autonomous agents.

    classify ─► policy_gate ─┬─ all intents blocked ─► template_reply ─────────────┐
                             └─ something answerable ─► retrieve ─► draft ─► post_check
                                                          ▲                      │
                                                          └── failed, retry once ┤
                                                                                 ▼
                                                     compose ◄───────────────────┘
                                                        │
                                                        ▼
                                                      route ─► audit ─► END

Why a fixed graph: the hard rules are enforced by code at fixed points (policy_gate BEFORE any
reply is written, post_check AFTER), so no model decides whether a rule applies.

PII is masked BEFORE the graph runs (tickets/service.py `intake`), so the graph state, LangSmith
traces and the LLM only ever see placeholders; the placeholder → value mapping never enters
the state. The background worker runs this graph (tickets/worker.py).

SKELETON: every node is a placeholder that records itself in `steps` and passes the state on.
Each later step replaces one placeholder with the real logic (see the TODO in each node).
View it: `uv run langgraph dev` (LangGraph Studio) or `uv run copilot graph` (Mermaid diagram).
"""

import functools
import operator
import time
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from copilot import telemetry as tel
from copilot.log import get_logger
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

    classification: Classification | None
    rule_signals: list[str]  # deterministic detectors (rules.py)
    blocked_intents: list[int]  # indexes into classification.intents
    template_parts: list[str]  # decline / neutral acknowledgement texts for blocked intents
    retrieved: list[tuple[str, float]]  # (chunk_id, score)
    draft: DraftOut | None
    draft_attempts: int
    post_check_failures: list[str]
    reasons: list[str]  # routing reason codes collected along the way
    # `steps` uses a reducer: each node APPENDS its name, so the path through the graph is visible
    steps: Annotated[list[str], operator.add]


# ---- Nodes (placeholders) -------------------------------------------------------------------


def classify(state: State) -> dict:
    # TODO step 2+3: deterministic rules (rules.py) + LLM call 1 (structured Classification)
    return {"steps": ["classify"], "classification": None, "rule_signals": []}


def policy_gate(state: State) -> dict:
    # TODO step 4: block intents that touch a hard rule (advice, complaint/KYC/AML decision,
    # vulnerable customer, security request). Code, not a prompt.
    return {"steps": ["policy_gate"], "blocked_intents": [], "template_parts": []}


def template_reply(state: State) -> dict:
    # TODO step 4: fixed decline / neutral acknowledgement (templates.py); no LLM writes it
    return {"steps": ["template_reply"]}


def retrieve(state: State) -> dict:
    # TODO step 7: top-k search with the existing Retriever (defective/promo chunks excluded)
    return {"steps": ["retrieve"], "retrieved": []}


def draft(state: State) -> dict:
    # TODO step 3: LLM call 2: answer only from the retrieved chunks, cite chunk IDs, or abstain
    return {
        "steps": ["draft"],
        "draft": None,
        "draft_attempts": state.get("draft_attempts", 0) + 1,
    }


def post_check(state: State) -> dict:
    # TODO step 5: numbers not in cited text, missing citations, advice wording, unknown chunk IDs
    return {"steps": ["post_check"], "post_check_failures": []}


def compose(state: State) -> dict:
    # TODO step 7: merge the grounded draft and any template parts; build page citations
    return {"steps": ["compose"], "reply": "", "citations": []}


def route(state: State) -> dict:
    # TODO step 6: every reason code that fires → auto_draft_ok or human_required + destination
    return {
        "steps": ["route"],
        "routing": Routing(decision="human_required", destination="support_agent", reasons=[]),
    }


def audit(state: State) -> dict:
    # TODO step 7: append one masked record to the audit log (the legal record, not LangSmith)
    return {"steps": ["audit"]}


# ---- Edges ----------------------------------------------------------------------------------


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
    """Each node gets an OpenTelemetry span (Grafana/Tempo) and a duration metric.
    LangSmith traces the same nodes on its own (LangGraph integration)."""

    @functools.wraps(fn)
    def run(state: State) -> dict:
        t0 = time.perf_counter()
        with tel.tracer.start_as_current_span(f"node.{name}"):
            out = fn(state)
        tel.NODE_MS.record((time.perf_counter() - t0) * 1000, {"node": name})
        return out

    return run


def build_graph():
    g = StateGraph(State, input_schema=InputState, output_schema=OutputState)
    for name, fn in [
        ("classify", classify),
        ("policy_gate", policy_gate),
        ("template_reply", template_reply),
        ("retrieve", retrieve),
        ("draft", draft),
        ("post_check", post_check),
        ("compose", compose),
        ("route", route),
        ("audit", audit),
    ]:
        g.add_node(name, _observed(name, fn))

    g.add_edge(START, "classify")
    g.add_edge("classify", "policy_gate")
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

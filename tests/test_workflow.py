"""Workflow shape (D3): node order and branches. Node logic is tested in its own module."""

from copilot.pipeline import workflow as wf
from copilot.pipeline.schemas import Classification, Intent

IN = {"ticket_id": "T1", "subject": "", "body": "What is the minimum deposit?"}


def test_answerable_ticket_takes_the_draft_path():
    out = wf.graph.invoke(IN)
    assert out["steps"] == [
        "classify",
        "policy_gate",
        "retrieve",
        "draft",
        "post_check",
        "compose",
        "route",
        "audit",
    ]


def _cls(n: int) -> Classification:
    i = Intent(topic="trading_advice_request", decision_requested="none", text="x")
    return Classification(
        category="trading_advice_request",
        intents=[i] * n,
        urgency="low",
        language="en",
        reply_language="en",
        sentiment="neutral",
        needs_account_action=False,
        vulnerable_customer=False,
        security_sensitive=False,
        search_query="",
        confidence=0.9,
    )


def test_fully_blocked_ticket_never_reaches_the_drafter():
    assert wf.after_gate({"classification": _cls(1), "blocked_intents": [0]}) == "template_reply"


def test_partly_blocked_ticket_still_drafts_the_allowed_part():
    assert wf.after_gate({"classification": _cls(2), "blocked_intents": [1]}) == "retrieve"


def test_failed_post_check_gets_exactly_one_redraft():
    assert wf.after_post_check({"post_check_failures": ["x"], "draft_attempts": 1}) == "draft"
    assert wf.after_post_check({"post_check_failures": ["x"], "draft_attempts": 2}) == "compose"
    assert wf.after_post_check({"post_check_failures": [], "draft_attempts": 1}) == "compose"

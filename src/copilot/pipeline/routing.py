"""Routing rules (PRD FR-5): which reason codes fire and where the ticket goes. Code, no prompt.

Every reason that fires is kept (an agent sees all of them, not just the first). The ticket goes
to the strictest destination any reason asks for:

    support_agent  <  senior_support  <  compliance

Reasons decided here today, from the implemented checks:

    spam_or_empty     pattern_check spam/empty            support_agent, LOW priority
    prompt_injection  the injection gate fired (an exact  senior_support, no AI draft
                      pattern or the injection model,
                      weak or strong; pipeline/injection.py)
    security_sensitive  an exact account-security pattern  senior_support
    no_draft          no AI draft exists and nothing      support_agent
                      above explains why

Reasons from later nodes (confidence_gate, policy_gate, post_check) arrive in `state["reasons"]`
and are merged in the same way. A KYC or AML decision request (policy_gate) sends the ticket to
compliance whatever the other reasons are. A ticket with no reason at all is `auto_draft_ok`
(an agent still reviews it before it is sent).

Injection threshold: any signal, even a weak one, blocks the AI draft. A false alarm costs an
agent a few minutes; a missed injection could put attacker text into a customer reply.
"""

from copilot.pipeline.schemas import Destination, ReasonCode, Routing

DESTINATION: dict[str, Destination] = {
    "spam_or_empty": "support_agent",
    "prompt_injection": "senior_support",
    "security_sensitive": "senior_support",
    "no_draft": "support_agent",
    "technical_failure": "support_agent",  # failed every attempt: a person takes it over
    # Filled in as their nodes are built (PRD FR-5.1 table):
    "hard_rule_topic": "senior_support",
    "vulnerable_customer": "senior_support",
    "angry_urgent": "senior_support",
    "multi_intent_blocked": "senior_support",
    "low_confidence": "senior_support",
    "low_resource_language": "support_agent",  # an agent who reads the language checks it
    "account_action_required": "support_agent",  # the agent checks the account, then sends
    "post_check_failed": "senior_support",
    "weak_retrieval": "support_agent",  # nothing relevant in the help pages: a person answers
    "not_in_kb": "support_agent",  # the drafter abstained or answered only part
    "kb_conflict": "support_agent",  # conservative answer drafted; the agent checks the point
}
_RANK = {"support_agent": 0, "senior_support": 1, "compliance": 2}


def injection_evidence(state: dict) -> list[str]:
    """Every injection signal: the gate's evidence (patterns, guard, head) and the 2nd line, the
    safety LLM (classify_safety). On the spam path the gate doesn't run, so exact injection
    patterns are read from pattern_check."""
    llm = ["llm:classify_safety"] if (state.get("safety") or {}).get("injection_suspected") else []
    if state.get("injection") is not None:
        return list(state["injection"].get("evidence", [])) + llm
    return llm + [
        f"pattern:{h['pattern']}"
        for h in (state.get("patterns") or {}).get("hits", [])
        if h["kind"] == "prompt_injection"
    ]


def decide(state: dict) -> Routing:
    reasons: list[ReasonCode] = []
    evidence: list[str] = []
    patterns = state.get("patterns") or {}

    if patterns.get("spam_reason"):
        reasons.append("spam_or_empty")
        evidence.append(f"spam:{patterns['spam_reason']}")
    if inj := injection_evidence(state):
        reasons.append("prompt_injection")
        evidence += inj
    sec = [h["pattern"] for h in patterns.get("hits", []) if h["kind"] == "security_sensitive"]
    if sec:
        reasons.append("security_sensitive")
        evidence += [f"pattern:{p}" for p in sec]
    for r in state.get("reasons", []):  # from later nodes, once they exist
        if r not in reasons:
            reasons.append(r)
    if not state.get("reply") and not reasons:
        reasons.append("no_draft")

    evidence += (state.get("policy") or {}).get("evidence", [])
    if not reasons:
        return Routing(decision="auto_draft_ok", destination=None, reasons=[], evidence=evidence)
    destination = max((DESTINATION.get(r, "senior_support") for r in reasons), key=_RANK.get)
    if (state.get("policy") or {}).get("compliance"):  # a KYC / AML decision: compliance decides
        destination = "compliance"
    return Routing(
        decision="human_required",
        destination=destination,
        reasons=reasons,
        priority="low" if reasons == ["spam_or_empty"] else "normal",
        evidence=evidence,
    )

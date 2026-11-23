"""Routing rules (PRD FR-5): which reason codes fire and where the ticket goes. Code, no prompt.

Every reason that fires is kept (an agent sees all of them, not just the first). The ticket goes
to the strictest destination any reason asks for:

    support_agent  <  senior_support  <  compliance

Reasons decided here today, from the implemented checks:

    spam_or_empty     pattern_check spam/empty            support_agent, LOW priority
    prompt_injection  ANY injection signal: an exact      senior_support, no AI draft
                      pattern or the risk classifier
                      (weak or strong)
    security_sensitive  an exact account-security pattern  senior_support
    no_draft          no AI draft exists and nothing      support_agent
                      above explains why

Reasons from later nodes (policy_gate, post_check, classify_*) arrive in `state["reasons"]` and
are merged in the same way. A ticket with no reason at all is `auto_draft_ok` (an agent still
reviews it before it is sent).

Injection threshold: any signal, even a weak one, blocks the AI draft. A false alarm costs an
agent a few minutes; a missed injection could put attacker text into a customer reply.
"""

from copilot.pipeline.schemas import Destination, ReasonCode, Routing

DESTINATION: dict[str, Destination] = {
    "spam_or_empty": "support_agent",
    "prompt_injection": "senior_support",
    "security_sensitive": "senior_support",
    "no_draft": "support_agent",
    # Filled in as their nodes are built (PRD FR-5.1 table):
    "hard_rule_topic": "senior_support",
    "vulnerable_customer": "senior_support",
    "angry_urgent": "senior_support",
    "multi_intent_blocked": "senior_support",
}
_RANK = {"support_agent": 0, "senior_support": 1, "compliance": 2}


def injection_evidence(state: dict) -> list[str]:
    """Every injection signal on the ticket, as detector name + score (never the text)."""
    out = [
        f"pattern:{h['pattern']}"
        for h in (state.get("patterns") or {}).get("hits", [])
        if h["kind"] == "prompt_injection"
    ]
    out += [
        f"classifier:prompt_injection {s['probability']:.2f} ({s['strength']})"
        for s in (state.get("risk") or {}).get("signals", [])
        if s["kind"] == "prompt_injection"
    ]
    return out


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

    if not reasons:
        return Routing(decision="auto_draft_ok", destination=None, reasons=[])
    destination = max((DESTINATION.get(r, "senior_support") for r in reasons), key=_RANK.get)
    return Routing(
        decision="human_required",
        destination=destination,
        reasons=reasons,
        priority="low" if reasons == ["spam_or_empty"] else "normal",
        evidence=evidence,
    )

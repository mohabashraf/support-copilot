"""policy_gate: the brief's hard rules, enforced in CODE before any reply is written (PRD FR-4).

Inputs, all already in the state: the merged Classification (intents + flags from the safety
LLM, the rest from triage), the local risk classifier's signals, and pattern_check's hits.
STRICTEST WINS: if any source flags something, it is blocked.

Per intent (FR-4.6), in this order:
    trading advice requested          → blocked, template `decline_advice`      (FR-4.1)
    decision requested: kyc / aml     → blocked, template `ack_<decision>`      (FR-4.2) compliance
    decision requested: complaint     → blocked, template `ack_complaint`       (FR-4.2)
    topic complaint / aml_compliance  → blocked, template `ack_complaint/aml`   (FR-4.2)
    anything else                     → allowed: grounded draft

Whole ticket (no per-intent attribution is reliable, so everything is blocked):
    vulnerable customer (LLM, or the classifier)  → ONLY `ack_vulnerable`, no product talk (FR-4.7)
    security-sensitive (LLM, classifier, pattern) → ONLY `ack_security`                    (FR-4.8)

The local classifier is a second opinion. A STRONG signal the LLM did not report blocks the
intents of the matching topic (or every intent, if none matches): we can't tell which part of
the ticket it means. A WEAK signal alone is shown to the agent as evidence, not acted on
(classifier/thresholds.toml: weak = "decided together with the LLM safety call").

Routing reasons emitted here: hard_rule_topic, multi_intent_blocked, vulnerable_customer,
security_sensitive, angry_urgent, account_action_required. KYC / AML decisions raise the
destination to compliance.
"""

from dataclasses import dataclass, field

from copilot.pipeline.schemas import Classification

# classifier risk → (rule name, the intent topic it belongs to)
_CLASSIFIER_RULE = {
    "trading_advice": ("advice", "trading_advice_request"),
    "complaint": ("complaint", "complaint"),
    "kyc_decision": ("kyc", "verification"),
    "aml_compliance": ("aml", "aml_compliance"),
}
TEMPLATE = {
    "advice": "decline_advice",
    "kyc": "ack_kyc",
    "aml": "ack_aml",
    "complaint": "ack_complaint",
}
COMPLIANCE_RULES = {"kyc", "aml"}  # deciding these belongs to compliance, not senior support


@dataclass
class IntentGate:
    index: int
    blocked: bool
    rule: str | None = None  # advice | kyc | aml | complaint | vulnerable | security
    template: str | None = None
    source: str | None = None  # llm | classifier | pattern


@dataclass
class PolicyResult:
    intents: list[IntentGate]
    ticket_rule: str | None = None  # vulnerable | security: the whole ticket is blocked
    templates: list[str] = field(default_factory=list)  # template keys, in reply order
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    compliance: bool = False

    @property
    def blocked(self) -> list[int]:
        return [g.index for g in self.intents if g.blocked]


def _llm_rule(topic: str, decision: str) -> str | None:
    if topic == "trading_advice_request":
        return "advice"
    if decision in ("kyc", "aml", "complaint"):
        return decision
    if topic == "complaint":
        return "complaint"
    if topic == "aml_compliance":
        return "aml"
    return None


def gate(c: Classification, risk: dict | None, pattern_hits: list[dict]) -> PolicyResult:
    gates = []
    for i, it in enumerate(c.intents):
        rule = _llm_rule(it.topic, it.decision_requested)
        src = "llm" if rule else None
        gates.append(IntentGate(i, rule is not None, rule, TEMPLATE.get(rule), src))
    r = PolicyResult(gates)

    signals = (risk or {}).get("signals", [])
    strong = {s["kind"]: s for s in signals if s["strength"] == "strong"}
    weak = {s["kind"]: s for s in signals if s["strength"] == "weak"}

    # second opinion: a strong classifier risk the LLM did not report
    for kind, (rule, topic) in _CLASSIFIER_RULE.items():
        if any(g.rule == rule for g in gates):
            continue  # the LLM found it too
        if kind in strong:
            targets = [g for g in gates if c.intents[g.index].topic == topic] or gates
            for g in targets:
                if not g.blocked:
                    g.blocked, g.rule, g.source = True, rule, "classifier"
                    g.template = TEMPLATE[rule]
            p = strong[kind]["probability"]
            r.evidence.append(f"classifier:{kind} {p} (not found by LLM)")
        elif kind in weak:
            r.evidence.append(f"classifier_weak:{kind} {weak[kind]['probability']} (shown only)")

    # the merged category (strictest of triage + intents) is a hard-rule topic no intent carries:
    # triage saw it, safety didn't (e.g. "account blocked, money gone" → AML vs only a complaint)
    cat_rule = _llm_rule(c.category, "none")
    if cat_rule and not any(g.rule == cat_rule for g in gates):
        topic = c.category
        targets = [g for g in gates if c.intents[g.index].topic == topic] or gates
        for g in targets:
            if g.rule != cat_rule:  # stricter rule replaces a laxer one on the same intent
                g.blocked, g.rule, g.source = True, cat_rule, "triage"
                g.template = TEMPLATE[cat_rule]
        r.evidence.append(f"triage:category {topic} (not in the safety intents)")

    # whole-ticket rules: vulnerable first (FR-4.7: no product talk at all), then security
    vulnerable = c.vulnerable_customer or "vulnerable_customer" in strong
    security = (
        c.security_sensitive
        or "security_sensitive" in strong
        or any(h["kind"] == "security_sensitive" for h in pattern_hits)
    )
    if vulnerable:
        r.ticket_rule = "vulnerable"
        r.reasons.append("vulnerable_customer")
        if not c.vulnerable_customer:
            r.evidence.append(f"classifier:vulnerable_customer "
                              f"{strong['vulnerable_customer']['probability']}")  # fmt: skip
    if security:
        r.reasons.append("security_sensitive")
        r.ticket_rule = r.ticket_rule or "security"
        if not c.security_sensitive and "security_sensitive" in strong:
            r.evidence.append(f"classifier:security_sensitive "
                              f"{strong['security_sensitive']['probability']}")  # fmt: skip
    if r.ticket_rule:  # every intent blocked, one template for the whole reply
        for g in gates:
            g.blocked, g.template = True, f"ack_{r.ticket_rule}"
            g.rule = g.rule or r.ticket_rule
            g.source = g.source or "llm"
        r.templates = [f"ack_{r.ticket_rule}"]
    else:
        seen = set()
        for g in gates:  # one template per rule, in intent order
            if g.blocked and g.template not in seen:
                seen.add(g.template)
                r.templates.append(g.template)

    hard = [g for g in gates if g.rule in TEMPLATE]
    if hard:
        r.reasons.append("hard_rule_topic")
    if r.blocked and len(r.blocked) < len(gates):
        r.reasons.append("multi_intent_blocked")
    r.compliance = any(g.rule in COMPLIANCE_RULES for g in gates)

    if c.sentiment == "angry" and c.urgency in ("high", "critical"):
        r.reasons.append("angry_urgent")
    if c.needs_account_action:
        r.reasons.append("account_action_required")
    return r

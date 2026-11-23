"""The injection gate (node `injection_check`): is this ticket trying to instruct the co-pilot?

Prompt injection is an attack on the co-pilot, not a customer topic, so it is not one of the
risks. It is decided on its own, right after pattern_check and BEFORE any LLM reads the ticket:

    layer 1  exact patterns      (pattern_check: "ignore previous instructions", "SYSTEM NOTE:")
    layer 2  injection model     (the risk classifier's injection head on bge-m3; any language)
    layer 3  Prompt Guard 2      (next: Meta's dedicated detector, once access is granted)
    ── after the gate ──
    2nd line the safety LLM call also flags injection; if it does, no draft (classify_safety)

ANY layer firing, even a weak model signal, means: no LLM call and no AI draft. The ticket goes
to a person (senior support, reason prompt_injection) with the evidence. A false alarm costs an
agent a few minutes; a missed injection could put attacker text into a customer reply.

Evidence is detector names and scores, never the customer's text (safe for monitoring).
"""

from dataclasses import asdict

from copilot.pipeline.risk_classifier import InjectionScore


def check(pattern_hits: list[dict], model: InjectionScore | None) -> dict:
    evidence = [f"pattern:{h['pattern']}" for h in pattern_hits if h["kind"] == "prompt_injection"]
    if model is not None and model.strength:
        evidence.append(f"model:{model.probability:.2f} ({model.strength})")
    return {
        "detected": bool(evidence),
        "evidence": evidence,
        "model": asdict(model) if model is not None else None,
    }

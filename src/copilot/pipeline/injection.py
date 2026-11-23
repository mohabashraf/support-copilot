"""The injection gate (node `injection_check`): is this ticket trying to instruct the co-pilot?

Prompt injection is an attack on the co-pilot, not a customer topic, so it is not one of the
risks. It is decided on its own, right after pattern_check and BEFORE any LLM reads the ticket:

    layer 1  exact patterns   pattern_check ("ignore previous instructions", "SYSTEM NOTE:").
                              Certain: a hit skips this gate and goes straight to routing.
    layer 2  guard model      NeuralTrust prompt-guard-oss-small (pipeline/injection_guard.py):
                              a dedicated multilingual detector. Fires at ≥ 0.5.
    layer 3  backup head      our injection head on bge-m3 (pipeline/risk_classifier.py). It
                              catches English "instructions to the AI" the guard misses, but
                              over-flags polite messages, so it fires only at ≥ 0.6.
    ── after the gate ──
    2nd line                  the safety LLM call also flags injection (classify_safety)

ANY layer firing means: no LLM call and no AI draft. The ticket goes to a person (senior
support, reason prompt_injection) with the evidence. Measured with `copilot eval injection`
(D12): patterns + guard + head ≥ 0.6 caught 18/18 at 2.0% false alarms (head alone: 4.8%).

Evidence is detector names and scores, never the customer's text (safe for monitoring):
    pattern:<rule>   guard:0.99 (strong)   head:0.66
"""

from dataclasses import asdict

from copilot.pipeline.injection_guard import GuardScore
from copilot.pipeline.risk_classifier import InjectionScore


def check(
    pattern_hits: list[dict],
    head: InjectionScore | None,
    guard: GuardScore | None,
    head_threshold: float,
) -> dict:
    evidence = [f"pattern:{h['pattern']}" for h in pattern_hits if h["kind"] == "prompt_injection"]
    if guard is not None and guard.strength:
        evidence.append(f"guard:{guard.probability:.2f} ({guard.strength})")
    if head is not None and head.probability >= head_threshold:
        evidence.append(f"head:{head.probability:.2f}")
    return {
        "detected": bool(evidence),
        "evidence": evidence,
        "guard": asdict(guard) if guard is not None else None,
        "model": asdict(head) if head is not None else None,  # the bge-m3 head (backup)
        "head_threshold": head_threshold,
    }

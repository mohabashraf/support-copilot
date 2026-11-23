"""Routing rules (pipeline/routing.py): every reason is kept, the strictest destination wins."""

from copilot.pipeline.routing import decide, injection_evidence

NO_HITS = {"hits": [], "spam_reason": None}


def test_nothing_fires_and_a_draft_exists_is_auto_draft_ok():
    r = decide({"patterns": NO_HITS, "reply": "The minimum deposit is 8 USD."})
    assert r.decision == "auto_draft_ok" and r.destination is None and r.reasons == []


def test_security_pattern_goes_to_senior_support_even_with_a_draft():
    hits = {
        "hits": [{"kind": "security_sensitive", "pattern": "disable_2fa"}],
        "spam_reason": None,
    }
    r = decide({"patterns": hits, "reply": "x"})
    assert r.reasons == ["security_sensitive"] and r.destination == "senior_support"
    assert r.evidence == ["pattern:disable_2fa"]


INJ = {"detected": True, "evidence": ["pattern:mode_switch"], "model": None}


def test_all_reasons_are_kept_and_the_strictest_destination_wins():
    r = decide({"patterns": NO_HITS, "injection": INJ, "reasons": ["hard_rule_topic"]})
    assert r.reasons == ["prompt_injection", "hard_rule_topic"]  # no_draft: already explained
    assert r.destination == "senior_support" and r.priority == "normal"


def test_spam_plus_injection_is_not_low_priority():
    r = decide({"patterns": {"hits": [], "spam_reason": "links"}, "injection": INJ})
    assert r.reasons == ["spam_or_empty", "prompt_injection"] and r.priority == "normal"


def test_injection_evidence_comes_from_the_gate():
    gate = {"detected": True, "evidence": ["pattern:system_note_to_ai", "model:0.67 (weak)"]}
    assert injection_evidence({"injection": gate}) == gate["evidence"]
    assert injection_evidence({}) == []

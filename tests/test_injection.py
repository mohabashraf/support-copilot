"""The injection gate (pipeline/injection.py): any layer fires → detected, evidence = names."""

from copilot.pipeline.injection import check
from copilot.pipeline.risk_classifier import InjectionScore

PATTERN = [{"kind": "prompt_injection", "pattern": "system_note_to_ai"}]
SECURITY = [{"kind": "security_sensitive", "pattern": "disable_2fa"}]


def test_pattern_alone_is_enough():
    g = check(PATTERN, InjectionScore(0.1, None, -1))
    assert g["detected"] and g["evidence"] == ["pattern:system_note_to_ai"]


def test_a_weak_model_signal_alone_is_enough():
    g = check([], InjectionScore(0.55, "weak", 2))
    assert g["detected"] and g["evidence"] == ["model:0.55 (weak)"]
    assert g["model"] == {"probability": 0.55, "strength": "weak", "unit": 2}


def test_both_layers_are_listed():
    assert check(PATTERN, InjectionScore(0.9, "strong", -1))["evidence"] == [
        "pattern:system_note_to_ai",
        "model:0.90 (strong)",
    ]


def test_clean_and_other_pattern_kinds_do_not_fire():
    g = check(SECURITY, InjectionScore(0.3, None, -1))
    assert not g["detected"] and g["evidence"] == []

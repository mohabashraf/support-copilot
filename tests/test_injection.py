"""The injection gate (pipeline/injection.py): patterns, the guard model (NeuralTrust) and our
bge-m3 head as a backup at a higher threshold. Any layer → detected; evidence = names + scores."""

from copilot.pipeline.injection import check
from copilot.pipeline.injection_guard import GuardScore
from copilot.pipeline.risk_classifier import InjectionScore

PATTERN = [{"kind": "prompt_injection", "pattern": "system_note_to_ai"}]
SECURITY = [{"kind": "security_sensitive", "pattern": "disable_2fa"}]
HEAD_LOW, HEAD_HIGH = InjectionScore(0.10, None, -1), InjectionScore(0.66, "weak", 2)
GUARD_LOW, GUARD_HIGH = GuardScore(0.01, None, "g"), GuardScore(0.99, "strong", "g")


def test_the_guard_alone_is_enough():
    g = check([], HEAD_LOW, GUARD_HIGH, head_threshold=0.6)
    assert g["detected"] and g["evidence"] == ["guard:0.99 (strong)"]


def test_the_head_is_a_backup_that_fires_only_above_its_own_threshold():
    assert check([], HEAD_HIGH, GUARD_LOW, head_threshold=0.6)["evidence"] == ["head:0.66"]
    # 0.55 is a weak head signal, but below the backup threshold: polite messages land here
    weak = InjectionScore(0.55, "weak", -1)
    assert not check([], weak, GUARD_LOW, head_threshold=0.6)["detected"]


def test_all_layers_are_listed():
    assert check(PATTERN, HEAD_HIGH, GUARD_HIGH, head_threshold=0.6)["evidence"] == [
        "pattern:system_note_to_ai",
        "guard:0.99 (strong)",
        "head:0.66",
    ]


def test_clean_and_other_pattern_kinds_do_not_fire():
    g = check(SECURITY, HEAD_LOW, GUARD_LOW, head_threshold=0.6)
    assert not g["detected"] and g["evidence"] == []
    assert g["guard"]["probability"] == 0.01 and g["model"]["probability"] == 0.1

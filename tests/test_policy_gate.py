"""policy_gate, template_reply, compose and routing: the hard rules in code (PRD FR-4)."""

from copilot.pipeline import policy
from copilot.pipeline import workflow as wf
from copilot.pipeline.schemas import Classification, Intent
from copilot.pipeline.templates import TEMPLATES
from tests.conftest import FakeChat, FakeInjectionGuard, FakeRiskClassifier


def _run(body: str, chat: FakeChat | None = None) -> dict:
    d = wf.Deps(FakeRiskClassifier(), FakeInjectionGuard(), chat or FakeChat())
    state: dict = {}
    for chunk in wf.graph.stream(
        {"ticket_id": "T", "subject": "", "body": body}, stream_mode="updates", context=d
    ):
        for update in chunk.values():
            for k, v in (update or {}).items():
                add = k in ("steps", "llm_usage", "reasons")
                state[k] = state.get(k, []) + v if add else v
    return state


def _c(*intents, **kw) -> Classification:
    base = {"category": "other", "urgency": "low", "language": "en", "reply_language": "en",
            "sentiment": "neutral", "needs_account_action": False, "vulnerable_customer": False,
            "security_sensitive": False, "search_query": "q", "confidence": 0.9}  # fmt: skip
    items = [Intent(topic=t, decision_requested=d, text="x") for t, d in intents]
    return Classification(intents=items, **{**base, **kw})


def _sig(kind, p=0.9, strength="strong"):
    return {"kind": kind, "probability": p, "strength": strength, "method": "classifier",
            "unit": -1, "nearest_example": "-"}  # fmt: skip


# ---- the graph ------------------------------------------------------------------------------


def test_trading_advice_gets_only_the_decline_template():
    s = _run("Should I buy gold now")
    assert s["blocked_intents"] == [0] and s["template_parts"] == ["decline_advice"]
    assert "retrieve" not in s["steps"] and "template_reply" in s["steps"]
    assert s["reply"] == TEMPLATES["decline_advice"]["en"]
    r = s["routing"]
    assert "hard_rule_topic" in r.reasons and r.destination == "senior_support"


def test_kyc_decision_goes_to_compliance_with_a_neutral_acknowledgement():
    s = _run("Please approve my documents today")
    assert s["reply"] == TEMPLATES["ack_kyc"]["en"]
    assert s["routing"].destination == "compliance"


def test_mixed_ticket_blocks_only_the_hard_rule_part():
    """KYC decision + an answerable question: the question still goes to retrieve/draft."""
    chat = FakeChat()
    s = _run("Please approve my documents", chat)
    assert s["policy"]["intents"][0]["rule"] == "kyc"
    # the fake safety model returns one intent; a 2-intent ticket is checked on policy.gate:
    r = policy.gate(_c(("verification", "kyc"), ("withdrawal", "none")), None, [])
    assert r.blocked == [0] and r.templates == ["ack_kyc"]
    assert {"hard_rule_topic", "multi_intent_blocked"} <= set(r.reasons) and r.compliance


def test_vulnerable_customer_gets_only_a_caring_acknowledgement():
    s = _run("I lost everything, how do I deposit again?")
    assert s["template_parts"] == ["ack_vulnerable"]
    assert s["reply"] == TEMPLATES["ack_vulnerable"]["en"]  # no product talk
    assert s["classification"].urgency == "critical"
    assert "vulnerable_customer" in s["routing"].reasons
    assert any("Vulnerable" in n for n in s["notes_for_agent"])


def test_security_request_from_a_pattern_blocks_the_whole_ticket():
    s = _run("Please change my registered email and tell me the minimum deposit")
    assert s["template_parts"] == ["ack_security"]
    assert "security_sensitive" in s["routing"].reasons


def test_arabic_reply_uses_the_arabic_template_other_languages_get_english_and_a_note():
    chat = FakeChat()
    chat.triage_overrides = {"reply_language": "ar", "language": "ar"}
    assert _run("Should I buy gold now", chat)["reply"] == TEMPLATES["decline_advice"]["ar"]
    chat.triage_overrides = {"reply_language": "es", "language": "es"}
    s = _run("Should I buy gold now", chat)
    assert s["reply"] == TEMPLATES["decline_advice"]["en"]
    assert any("no approved es text" in n for n in s["notes_for_agent"])


def test_a_clean_question_is_allowed_and_gets_a_grounded_draft():
    s = _run("What is the minimum deposit?")
    assert s["blocked_intents"] == [] and {"retrieve", "draft"} <= set(s["steps"])
    assert s["reply"] == "Per our help page (faq#min-deposit)."
    assert s["citations"][0]["chunk_id"] == "faq#min-deposit"
    assert s["routing"].decision == "auto_draft_ok"


# ---- policy.gate ----------------------------------------------------------------------------


def test_process_question_about_verification_is_allowed():
    r = policy.gate(_c(("verification", "none")), None, [])
    assert r.blocked == [] and r.reasons == []


def test_complaint_and_aml_topics_are_blocked_even_without_a_decision():
    r = policy.gate(_c(("complaint", "none"), ("aml_compliance", "none")), None, [])
    assert r.blocked == [0, 1] and r.templates == ["ack_complaint", "ack_aml"]
    assert r.compliance  # AML → compliance


def test_strong_classifier_signal_the_llm_missed_blocks_the_matching_intent():
    risk = {"signals": [_sig("kyc_decision")]}
    r = policy.gate(_c(("verification", "none"), ("deposit", "none")), risk, [])
    assert r.blocked == [0] and r.intents[0].source == "classifier"
    assert r.evidence == ["classifier:kyc_decision 0.9 (not found by LLM)"]


def test_strong_signal_with_no_matching_intent_blocks_everything():
    r = policy.gate(_c(("deposit", "none")), {"signals": [_sig("trading_advice")]}, [])
    assert r.blocked == [0] and r.templates == ["decline_advice"]


def test_weak_classifier_signal_alone_is_shown_not_acted_on():
    r = policy.gate(_c(("deposit", "none")), {"signals": [_sig("complaint", 0.55, "weak")]}, [])
    assert r.blocked == [] and r.evidence == ["classifier_weak:complaint 0.55 (shown only)"]


def test_angry_urgent_and_account_action_are_routing_reasons():
    c = _c(("withdrawal", "none"), sentiment="angry", urgency="critical",
           needs_account_action=True)  # fmt: skip
    r = policy.gate(c, None, [])
    assert r.blocked == [] and r.reasons == ["angry_urgent", "account_action_required"]


def test_triage_category_stricter_than_every_intent_applies_its_rule():
    """Real case: safety saw only a complaint, triage saw AML (account blocked): AML wins,
    compliance decides."""
    r = policy.gate(_c(("complaint", "complaint"), category="aml_compliance"), None, [])
    assert r.intents[0].rule == "aml" and r.templates == ["ack_aml"] and r.compliance
    assert r.evidence == ["triage:category aml_compliance (not in the safety intents)"]

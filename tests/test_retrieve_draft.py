"""retrieve + draft + compose with fakes (no network). Real quality: the end-to-end eval."""

import pytest

from copilot.pipeline import grounding
from copilot.pipeline import workflow as wf
from copilot.pipeline.llm import LLMOutputError
from copilot.pipeline.schemas import Classification, DraftOut, Intent
from copilot.pipeline.templates import TEMPLATES
from tests.conftest import FakeChat, FakeInjectionGuard, FakeRetriever, FakeRiskClassifier


class _Draft(FakeChat):
    """Fixed draft answer; everything else as FakeChat."""

    def __init__(self, out: DraftOut | None = None, fail: bool = False):
        super().__init__()
        self.out, self.fail, self.prompts = out, fail, []

    def invoke(self, messages, config=None, schema=None):
        if schema is DraftOut:
            self.prompts.append(messages[-1].content)
            r = super().invoke(messages, config, schema)
            return {**r, "parsed": self.out} if self.out else r
        return super().invoke(messages, config, schema)


def _run(body: str, draft_llm=None, chat=None) -> dict:
    d = wf.Deps(FakeRiskClassifier(), FakeInjectionGuard(), chat or FakeChat(),
                retriever=FakeRetriever(), draft_llm=draft_llm or FakeChat())  # fmt: skip
    state: dict = {}
    for chunk in wf.graph.stream(
        {"ticket_id": "T", "subject": "", "body": body}, stream_mode="updates", context=d
    ):
        for update in chunk.values():
            for k, v in (update or {}).items():
                add = k in ("steps", "llm_usage", "reasons")
                state[k] = state.get(k, []) + v if add else v
    return state


def _d(**kw) -> DraftOut:
    base = {"abstain": False, "partial": False, "reply": "x", "cited_chunk_ids": [],
            "uncovered": [], "note_for_agent": ""}  # fmt: skip
    return DraftOut(**{**base, **kw})


def test_weak_retrieval_skips_the_draft_call():
    llm = _Draft()
    s = _run("zzz qqq xxyy", llm)
    assert "draft" not in s["steps"] and s["sources"] == [] and not llm.prompts
    assert s["routing"].reasons == ["weak_retrieval"] and s.get("reply") is None


def test_defective_promo_and_compliance_chunks_never_reach_the_drafter():
    llm = _Draft()
    _run("How do I withdraw? Trade from every device. Important access notice", llm)
    sent = llm.prompts[0]
    for cid in ("withdraw#steps", "promo#devices", "notice#access"):
        assert f'id="{cid}"' not in sent


def test_citing_a_chunk_that_was_not_given_is_retried_then_fails_closed():
    llm = _Draft(_d(reply="Fees are 0%.", cited_chunk_ids=["faq#made-up"]))
    with pytest.raises(LLMOutputError, match="not in the sources"):
        _run("What is the minimum deposit?", llm)
    assert len(llm.prompts) == 2  # one retry inside the graph


def test_abstain_routes_not_in_kb_and_gives_no_reply():
    s = _run("What is the minimum deposit?", _Draft(_d(abstain=True, reply="")))
    assert "not_in_kb" in s["routing"].reasons and s.get("reply") is None


def test_citing_a_conflict_chunk_flags_kb_conflict():
    out = _d(reply="Up to 60 minutes.", cited_chunk_ids=["faq#deposit-credit-time"],
             note_for_agent="Pages disagree: instant vs 60 minutes.")  # fmt: skip
    s = _run("How long does it take for deposited funds to be credited?", _Draft(out))
    assert "kb_conflict" in s["routing"].reasons
    assert any("Pages disagree" in n for n in s["notes_for_agent"])


def test_partial_answer_is_drafted_with_the_gap_as_a_note_not_routed():
    out = _d(partial=True, reply="The minimum is in the FAQ.", cited_chunk_ids=["faq#min-deposit"],
             uncovered=["crypto fees"])  # fmt: skip
    s = _run("What is the minimum deposit and the crypto fee?", _Draft(out))
    assert "not_in_kb" not in s["routing"].reasons  # FR-3.2b: answer the part, flag the gap
    assert "Not covered by the help pages: crypto fees" in s["notes_for_agent"]


def test_mixed_ticket_drafts_only_the_allowed_part_and_composes_in_intent_order():
    """Template for the KYC part first (intent 0), then the grounded answer (intent 1)."""
    c = Classification(
        category="verification", urgency="low", language="en", reply_language="en",
        sentiment="neutral", needs_account_action=False, vulnerable_customer=False,
        security_sensitive=False, search_query="minimum deposit", confidence=0.9,
        intents=[Intent(topic="verification", decision_requested="kyc", text="approve me"),
                 Intent(topic="deposit", decision_requested="none", text="minimum deposit?")],
    )  # fmt: skip
    assert grounding.queries(c, blocked=[0]) == ["minimum deposit", "minimum deposit?"]
    state = {
        "classification": c,
        "policy": {"intents": [{"index": 0, "blocked": True, "template": "ack_kyc"},
                               {"index": 1, "blocked": False, "template": None}],
                   "ticket_rule": None},
        "template_texts": [{"key": "ack_kyc", "text": TEMPLATES["ack_kyc"]["en"]}],
        "draft": _d(reply="The minimum deposit is in our FAQ.",
                    cited_chunk_ids=["faq#min-deposit"]),
        "sources": [grounding.source(FakeRetriever().search("minimum deposit", 1)[0])],
    }  # fmt: skip
    out = wf.compose(state)
    assert out["reply"] == TEMPLATES["ack_kyc"]["en"] + "\n\nThe minimum deposit is in our FAQ."
    assert out["citations"][0]["chunk_id"] == "faq#min-deposit"
    assert out["citations"][0]["page_url"].startswith("https://valetax.com/")


def test_the_drafter_is_told_how_many_parts_are_handled_separately():
    llm = _Draft()
    chat = FakeChat()
    _run("Please approve my documents. What is the minimum deposit?", llm, chat)
    # the fake safety model returns one intent (approve → kyc): everything blocked, no draft
    assert not llm.prompts


def test_check_draft_rules():
    ids = {"faq#min-deposit"}
    with pytest.raises(LLMOutputError, match="no reply"):
        grounding.check_draft(_d(reply="  "), ids, True)
    with pytest.raises(LLMOutputError, match="without any citation"):
        grounding.check_draft(_d(reply="It is 10 USD."), ids, True)
    assert grounding.check_draft(_d(reply="Thanks!"), set(), False)  # no question, no sources
    assert grounding.check_draft(_d(abstain=True, reply=""), ids, True)


def test_mixed_ticket_without_a_grounded_answer_keeps_the_template_and_says_what_is_missing():
    c = Classification(
        category="verification", urgency="low", language="en", reply_language="en",
        sentiment="neutral", needs_account_action=False, vulnerable_customer=False,
        security_sensitive=False, search_query="withdraw to visa", confidence=0.9,
        intents=[Intent(topic="verification", decision_requested="kyc", text="approve me"),
                 Intent(topic="withdrawal", decision_requested="none", text="withdraw to Visa?")],
    )  # fmt: skip
    state = {
        "classification": c,
        "policy": {"intents": [{"index": 0, "blocked": True, "template": "ack_kyc"},
                               {"index": 1, "blocked": False, "template": None}]},
        "template_texts": [{"key": "ack_kyc", "text": TEMPLATES["ack_kyc"]["en"]}],
        "draft": _d(abstain=True, reply=""),
    }  # fmt: skip
    out = wf.compose(state)
    assert out["reply"] == TEMPLATES["ack_kyc"]["en"]
    assert out["notes_for_agent"] == ['Draft is INCOMPLETE: add an answer to "withdraw to Visa?"']


def test_low_resource_language_gets_an_english_draft_for_the_agent():
    llm = _Draft(_d(reply="It starts from 8 USD.", cited_chunk_ids=["faq#min-deposit"]))
    chat = FakeChat()
    chat.triage_overrides = {"language": "ckb", "reply_language": "ckb"}
    s = _run("What is the minimum deposit?", llm, chat)
    assert "<reply_language>en</reply_language>" in llm.prompts[0]
    assert "low_resource_language" in s["routing"].reasons
    assert any("Draft is in English (ckb" in n for n in s["notes_for_agent"])
    assert s["post_check_failures"] == [] and s["reply"]  # checked as English (eval KU1 bug)


def test_partial_with_a_sensitive_gap_goes_to_a_person():
    """Real case X27: 'Islamic accounts, so is forex halal?' → fact answered, ruling declined."""
    out = _d(partial=True, reply="Our accounts are described as Islamic-friendly.",
             cited_chunk_ids=["open#faq-islamic"], uncovered=["religious ruling"],
             gap_needs_person=True)  # fmt: skip
    s = _run("Do you offer an Islamic-friendly account? Is forex halal?", _Draft(out))
    assert "not_in_kb" in s["routing"].reasons and s["reply"]

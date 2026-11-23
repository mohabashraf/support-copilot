"""post_check: deterministic checks on the draft + the one-redraft loop (fakes, no network)."""

import pytest

from copilot.pipeline import post_check as pc
from copilot.pipeline import workflow as wf
from copilot.pipeline.schemas import DraftOut
from tests.conftest import FakeChat, FakeInjectionGuard, FakeRetriever, FakeRiskClassifier

SRC = [{"id": "faq#min-deposit", "section": "What is the minimum deposit amount?",
        "text": "The minimum deposit starts from 8 USD, depending on the account type.",
        "flags": [], "pages": []},
       {"id": "open#faq-leverage", "section": "What leverage settings should I select?",
        "text": "Leverage of up to 1:2,000 is available depending on the account type.",
        "flags": ["rule_sensitive"], "pages": []}]  # fmt: skip


def _d(reply, cited=("faq#min-deposit",), **kw) -> DraftOut:
    base = {"abstain": False, "partial": False, "uncovered": [], "note_for_agent": ""}
    return DraftOut(reply=reply, cited_chunk_ids=list(cited), **{**base, **kw})


def _check(reply, cited=("faq#min-deposit",), ticket="hi", lang="en"):
    return pc.check(_d(reply, cited), SRC, ticket, lang)


# ---- numbers --------------------------------------------------------------------------------


def test_numbers_must_be_in_a_cited_chunk():
    assert _check("The minimum deposit starts from 8 USD.") == []
    assert _check("The minimum deposit is 10 USD.") == ["unsupported_number:10"]
    # in the KB, but not in a CITED chunk: still unsupported
    assert _check("Leverage is up to 1:2000.") == ["unsupported_number:1,2000"]
    assert _check("Leverage is up to 1:2000.", cited=("open#faq-leverage",)) == []


def test_digits_are_normalised_before_comparing():
    assert (
        pc.normalise_digits("٨ دولار · ۸ · ๘ · 1,000 · ١٬٠٠٠") == "8 دولار · 8 · 8 · 1000 · 1000"
    )
    assert _check("الحد الأدنى للإيداع يبدأ من ٨ دولار.", lang="ar") == []
    assert _check("الحد الأدنى للإيداع ٩ دولار.", lang="ar") == ["unsupported_number:9"]
    assert _check("Up to 1:2000.", cited=("open#faq-leverage",)) == []  # KB writes 1:2,000


def test_placeholders_are_not_numbers_and_must_come_from_the_ticket():
    assert _check("Hello [NAME_1], it starts from 8 USD.", ticket="I am [NAME_1]") == []
    assert _check("Hello [NAME_2], 8 USD.", ticket="I am [NAME_1]") == [
        "unknown_placeholder:[NAME_2]"
    ]


# ---- wording --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "We recommend buying gold now.",
        "You should use higher leverage.",
        "Gold will go up this week.",
        "It's a good time to buy EURUSD.",
        "ننصحك بشراء الذهب الآن",
        "سيرتفع الذهب غدا",
    ],
)
def test_advice_wording_fails(reply):
    lang = "ar" if any("؀" <= ch <= "ۿ" for ch in reply) else "en"
    assert any(f.startswith("advice_language:") for f in _check(reply, cited=(), lang=lang))


@pytest.mark.parametrize(
    "reply",
    [
        "You need to verify your account before trading.",
        "Your funds should reach your account soon.",
        "We recommend contacting support if your funds have not arrived.",
        "We can't guarantee a specific time.",
        "The pages do not state a guaranteed processing time.",  # eval X09
        "Withdrawals are not guaranteed within an hour.",
        "Please choose the account type that suits you on the accounts page.",
    ],
)
def test_normal_support_sentences_pass(reply):
    assert _check(reply, cited=()) == []


@pytest.mark.parametrize(
    "reply",
    [
        "Your account has been approved.",
        "We guarantee it today.",
        "تمت الموافقة على طلبك",
        "Withdrawals are guaranteed within 1 hour.",
        "It is guaranteed to arrive today.",
    ],
)
def test_promises_and_account_claims_fail(reply):
    lang = "ar" if "ت" in reply else "en"
    assert any(f.startswith("promise:") for f in _check(reply, cited=(), lang=lang))


def test_reply_script_must_match_the_reply_language():
    assert _check("The minimum deposit starts from 8 USD.", lang="ar") == [
        "wrong_language:latin for ar"
    ]
    assert _check("الحد الأدنى ٨ دولار", lang="en") == ["wrong_language:arabic for en"]


def test_abstained_draft_is_not_checked():
    assert pc.check(_d("", cited=(), abstain=True), SRC, "hi", "en") == []


# ---- the redraft loop through the graph -----------------------------------------------------


class _Seq(FakeChat):
    """Returns the given drafts in order; records each draft prompt."""

    def __init__(self, *drafts):
        super().__init__()
        self.drafts, self.prompts = list(drafts), []

    def invoke(self, messages, config=None, schema=None):
        r = super().invoke(messages, config, schema)
        if schema is DraftOut:
            self.prompts.append(messages[-1].content)
            return {**r, "parsed": self.drafts.pop(0)}
        return r


def _run(body, draft_llm) -> dict:
    d = wf.Deps(FakeRiskClassifier(), FakeInjectionGuard(), FakeChat(),
                retriever=FakeRetriever(), draft_llm=draft_llm)  # fmt: skip
    state: dict = {}
    for chunk in wf.graph.stream(
        {"ticket_id": "T", "subject": "", "body": body}, stream_mode="updates", context=d
    ):
        for update in chunk.values():
            for k, v in (update or {}).items():
                add = k in ("steps", "llm_usage", "reasons")
                state[k] = state.get(k, []) + v if add else v
    return state


GOOD = _d("It starts from 8 USD, depending on the account type.")
BAD = _d("It is 10 USD and we recommend buying gold.")


def test_a_failed_draft_is_redrafted_once_with_the_failures_as_feedback():
    llm = _Seq(BAD, GOOD)
    s = _run("What is the minimum deposit?", llm)
    assert s["steps"].count("draft") == 2 and s["steps"].count("post_check") == 2
    assert "<feedback>" not in llm.prompts[0] and "unsupported_number:10" in llm.prompts[1]
    assert s["reply"] == GOOD.reply and s["post_check_failures"] == []
    assert s["routing"].decision == "auto_draft_ok"


def test_two_failed_drafts_withhold_the_draft_and_go_to_a_person():
    s = _run("What is the minimum deposit?", _Seq(BAD, BAD))
    assert s["steps"].count("draft") == 2
    assert s.get("reply") is None  # the failed text is never shown as the reply
    assert "post_check_failed" in s["routing"].reasons
    assert s["routing"].destination == "senior_support"
    assert any(n.startswith("AI draft withheld") for n in s["notes_for_agent"])

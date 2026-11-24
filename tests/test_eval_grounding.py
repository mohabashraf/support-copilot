"""End-to-end eval scoring for FR-3 (never invent policy, fees or timelines): a number in the
final reply must be in a chunk the reply cites, or in a fixed template. Checked by the eval
itself, independently of the pipeline's post_check."""

from copilot.eval import e2e

TICKET = {
    "id": "T1",
    "suite": "labelled",
    "subject": "Fees",
    "body": "What is the minimum deposit, and are there deposit fees?",
    "expected": {"category": "deposit", "must_route": False, "destination": None},
}
SOURCE = {
    "id": "open#faq-min-initial-deposit",
    "section": "Minimum initial deposit",
    "text": "The minimum deposit starts from 8 USD, depending on the account type.",
    "flags": [],
}
CITATION = {"chunk_id": SOURCE["id"], "page_title": "Open", "page_url": "u", "section": "s"}


def result(reply: str, cited: bool = True) -> dict:
    return {
        "id": "T1",
        "suite": "labelled",
        "error": None,
        "routing": {"decision": "auto_draft_ok", "destination": None, "reasons": []},
        "classification": {},
        "reply": reply,
        "draft": {
            "abstain": False,
            "partial": True,
            "reply": reply,
            "cited_chunk_ids": [SOURCE["id"]] if cited else [],
        },  # fmt: skip
        "citations": [CITATION] if cited else [],
        "sources": [SOURCE],
        "usage": [],
        "ms": 1,
    }


def test_numbers_from_the_cited_chunk_pass():
    s = e2e.score(TICKET, result("The minimum deposit starts from 8 USD. We do not have "
                                 "information about deposit fees."))  # fmt: skip
    assert s["violations"] == [] and s["uncited_numbers"] == []


def test_an_invented_fee_or_timeline_is_a_hard_rule_violation():
    s = e2e.score(TICKET, result("The minimum deposit is 8 USD and the fee is 3.5%, "
                                 "credited within 2 hours."))  # fmt: skip
    assert s["uncited_numbers"] == ["2", "3.5"]
    assert "uncited_number:2,3.5" in s["violations"]
    assert e2e.summarise([s])["release_gate"] is False


def test_a_number_without_any_citation_is_a_violation():
    s = e2e.score(TICKET, result("The minimum deposit is 8 USD.", cited=False))
    assert s["uncited_numbers"] == ["8"] and s["draft_without_citation"] is True


def test_a_reply_with_no_facts_and_no_citation_is_listed_not_failed():
    s = e2e.score(TICKET, result("Thank you for your kind message.", cited=False))
    assert s["violations"] == [] and s["draft_without_citation"] is True
    assert e2e.summarise([s])["drafts_without_citation"] == ["T1"]

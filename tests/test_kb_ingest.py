"""Ingestion must produce exactly the reviewed chunk plan, without changing the source text."""

import json
from pathlib import Path

import pytest

from copilot.kb.ingest import KB_DIR, IngestError, build_kb
from copilot.kb.parse import normalise

PLAN = json.loads((KB_DIR / "chunk_plan.json").read_text(encoding="utf-8"))
DEPOSIT = "https://valetax.com/how-to-deposit/"
WITHDRAW = "https://valetax.com/how-to-withdraw/"
ALL_PAGES = {
    DEPOSIT,
    WITHDRAW,
    "https://valetax.com/how-to-open-an-account/",
    "https://valetax.com/how-to-verify-account/",
}


@pytest.fixture(scope="module")
def kb():
    return build_kb()


def test_chunk_ids_match_reviewed_plan(kb):
    assert {c.id for c in kb.chunks} == {c["id"] for c in PLAN}
    assert len(kb.chunks) == len(PLAN) == 26


def test_chunk_sources_match_plan(kb):
    for planned in PLAN:
        assert set(kb.get(planned["id"]).page_urls) == set(planned["pages"]), planned["id"]


def test_flags_match_plan(kb):
    for planned in PLAN:
        assert set(kb.get(planned["id"]).flags) == set(planned["flags"]), planned["id"]


def test_shared_faq_stored_once_with_both_pages(kb):
    ids = [c.id for c in kb.chunks]
    assert ids.count("faq#min-deposit") == 1
    assert set(kb.get("faq#min-deposit").page_urls) == {DEPOSIT, WITHDRAW}


def test_access_notice_on_every_page(kb):
    assert set(kb.get("notice#access").page_urls) == ALL_PAGES


def test_source_text_is_not_rewritten(kb):
    """Every chunk's text must appear in its page's own text (only whitespace is normalised)."""
    from bs4 import BeautifulSoup

    files = {
        "https://valetax.com/how-to-deposit/": "how-to-deposit.html",
        "https://valetax.com/how-to-withdraw/": "how-to-withdraw.html",
        "https://valetax.com/how-to-open-an-account/": "how-to-open-an-account.html",
        "https://valetax.com/how-to-verify-account/": "how-to-verify-account.html",
    }
    page_text = {
        url: normalise(BeautifulSoup((KB_DIR / "raw" / f).read_text(encoding="utf-8"), "html.parser").get_text(" "))
        for url, f in files.items()
    }
    key_facts = {
        "faq#min-deposit": "8 USD",
        "faq#deposit-credit-time": "within 60 minutes",
        "faq#withdrawal-not-received": "24 hours",
        "open#faq-documents": "not older than 6 months",
        "open#faq-opening-time": "48 hours",
        "open#faq-leverage": "1:2000",
    }
    for cid, fact in key_facts.items():
        chunk = kb.get(cid)
        assert fact in chunk.text, cid
        for url in chunk.page_urls:
            assert fact in page_text[url], (cid, url)


def test_known_inconsistencies_are_preserved_not_fixed(kb):
    # The KB is used "as it is": defects stay in, flagged.
    assert "amount to deposit" in kb.get("withdraw#steps").text
    assert "Start Trading!" in kb.get("withdraw#steps").text
    assert "3 quick and simple steps" in kb.get("open#intro").text
    assert "Step 4" in kb.get("open#steps").text  # 4 steps despite "3 steps"


def test_hash_is_stable(kb):
    assert build_kb().kb_hash == kb.kb_hash


def test_unknown_faq_fails_loudly(tmp_path: Path):
    """A new FAQ on the site must get an ID on purpose, not silently."""
    (tmp_path / "raw").mkdir()
    for f in (KB_DIR / "raw").iterdir():
        (tmp_path / "raw" / f.name).write_bytes(f.read_bytes())
    cfg = (KB_DIR / "kb_config.toml").read_text(encoding="utf-8")
    cfg = cfg.replace('"Do you offer an Islamic-friendly account?" = "open#faq-islamic"\n', "")
    (tmp_path / "kb_config.toml").write_text(cfg, encoding="utf-8")
    with pytest.raises(IngestError, match="FAQ not in kb_config"):
        build_kb(tmp_path)

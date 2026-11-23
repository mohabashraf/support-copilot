"""Risk classifier LOGIC (strong/weak thresholds, which sentence, evidence, category margin).
Quality is measured by `copilot eval risks` with the real bge-m3 model."""

import numpy as np

from copilot.pipeline.risk_classifier import RISKS, RiskClassifier, sentences

WORDS = ["buy", "sell", "gold", "complaint", "fraud", "approve", "verify", "deposit", "minimum",
         "hello", "leverage", "maximum", "recommend"]  # fmt: skip


class BagEmbedder:
    """Tiny deterministic embedder: normalised word counts over WORDS."""

    def embed_queries(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(w) for w in WORDS], dtype=np.float32) + 1e-3
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


EXAMPLES = [
    {
        "text": "should I buy gold",
        "risks": ["trading_advice"],
        "category": "trading_advice_request",
    },
    {
        "text": "recommend buy or sell",
        "risks": ["trading_advice"],
        "category": "trading_advice_request",
    },
    {"text": "this is fraud complaint", "risks": ["complaint"], "category": "complaint"},
    {"text": "approve verify now", "risks": ["kyc_decision"], "category": "verification"},
    {"text": "minimum deposit", "risks": [], "category": "deposit"},
    {"text": "maximum leverage", "risks": [], "category": "account_opening"},
    {"text": "hello", "risks": [], "category": "other"},
]
THRESHOLDS = {k: {"weak": 0.5, "strong": 0.8} for k in RISKS}


def _clf():
    return RiskClassifier(BagEmbedder(), EXAMPLES, THRESHOLDS)


def test_sentence_split_handles_numbered_and_multilingual_text():
    assert sentences("1) What documents? 2) Approve it now.") == [
        "1) What documents?",
        "2) Approve it now.",
    ]
    assert sentences("Hello. السلام عليكم؟ 你好。OK") == [
        "Hello.",
        "السلام عليكم؟",
        "你好。",
        "OK",
    ]


def test_risky_sentence_is_found_and_has_evidence():
    r = _clf().predict("What is the minimum deposit? Also should I buy gold now?")
    sig = next(s for s in r.signals if s.kind == "trading_advice")
    assert sig.unit == 1  # the second sentence, not the deposit question
    assert sig.nearest_example in {"should I buy gold", "recommend buy or sell"}
    assert sig.method == "classifier" and 0.5 <= sig.probability <= 1


def test_strength_follows_thresholds():
    clf = _clf()
    p = clf.predict("buy gold")
    sig = next(s for s in p.signals if s.kind == "trading_advice")
    assert sig.strength == ("strong" if sig.probability >= 0.8 else "weak")
    clf.thresholds["trading_advice"] = {"weak": 0.99, "strong": 0.999}
    assert "trading_advice" not in clf.predict("buy gold").kinds  # below the weak threshold


def test_clean_ticket_has_no_signals_and_a_category():
    r = _clf().predict("minimum deposit")
    assert r.signals == [] and r.category == "deposit" and r.category_margin > 0
    assert set(r.probabilities) == set(RISKS)

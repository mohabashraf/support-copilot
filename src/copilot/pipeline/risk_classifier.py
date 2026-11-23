"""Risk classifier: logistic regression heads on frozen bge-m3 embeddings (D12).

    ticket (masked) ─► sentences + whole text ─► bge-m3 (already loaded for KB search)
                    ─► one logistic-regression head per risk  → probability per risk
                    ─► one multinomial head for category      → probability per category

Why: independent of the LLM (deterministic, local, free, ~50 ms), multilingual through bge-m3
(trained on English + Arabic, transfers to other languages; measured per language by
`copilot eval risks`), and it gives a PROBABILITY per risk instead of a yes/no keyword match.
Hard negatives in the training set ("what is the maximum leverage?" = not advice; "not a
complaint, just asking" = not a complaint) teach it the boundary that plain similarity misses.

Exact phrases (prompt injection, account security) and spam run BEFORE this, in patterns.py
(node pattern_check), so they don't depend on this model.

Prompt injection is NOT one of the risks: it is an attack on the co-pilot, not a customer
topic, and it is decided earlier, by its own gate (node injection_check, pipeline/injection.py),
before any LLM reads the ticket. This module only provides the injection head's score for that
gate (`injection()`); `predict()` returns the customer risks.

Evidence on every signal: the probability, which part of the ticket (sentence index), and the
nearest training example of that risk (our own text, never the customer's).

Training data: classifier/training_examples.jsonl (written by us; production retrains on real,
agent-labelled tickets). Thresholds: classifier/thresholds.toml, set from the eval.
"""

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

import numpy as np
from sklearn.linear_model import LogisticRegression

from copilot.config import ROOT
from copilot.log import get_logger
from copilot.retrieval.embeddings import Embedder

log = get_logger(__name__)

RISKS = (
    "trading_advice",
    "complaint",
    "kyc_decision",
    "aml_compliance",
    "vulnerable_customer",
    "security_sensitive",
)
INJECTION = "prompt_injection"  # its own head, read by the injection gate (not a customer risk)
HEADS = (*RISKS, INJECTION)
TRAINING = ROOT / "classifier" / "training_examples.jsonl"
THRESHOLDS = ROOT / "classifier" / "thresholds.toml"
CACHE = ROOT / ".cache"

Strength = Literal["strong", "weak"]


@dataclass(frozen=True)
class RiskSignal:
    kind: str
    probability: float
    strength: Strength
    method: Literal["classifier"]
    unit: int  # -1 = whole ticket, else sentence index
    nearest_example: str  # closest training example of this risk (our text, not the customer's)


@dataclass(frozen=True)
class InjectionScore:
    probability: float  # highest over the whole ticket and each sentence
    strength: Strength | None  # None = below the weak threshold
    unit: int  # -1 = whole ticket, else sentence index


@dataclass
class RiskResult:
    signals: list[RiskSignal] = field(default_factory=list)
    probabilities: dict[str, float] = field(default_factory=dict)  # highest per risk
    category: str | None = None
    category_probability: float = 0.0
    category_margin: float = 0.0  # gap to the second-best category: a confidence proxy

    @property
    def kinds(self) -> list[str]:
        return sorted({s.kind for s in self.signals})

    def strong(self) -> list[str]:
        return sorted({s.kind for s in self.signals if s.strength == "strong"})


# Split after . ! ? ؟ followed by space; after CJK 。！？ even without a space; on newlines; and
# before numbered items ("1) … 2) …").
_SPLIT = re.compile(r"(?<=[.!?؟])\s+|(?<=[。！？])\s*|\n+|\s+(?=\d\)\s)")


def score(raw: float, t: dict[str, float]) -> tuple[float, Strength | None]:
    """Round to 2 decimals FIRST, then compare with the thresholds: the number shown and its
    label always agree (0.795 is shown as 0.80, so it is strong, never "0.80 (weak)")."""
    # Half-up on the decimal value (Python's round() gives 0.49 for 0.495: binary floats).
    p = float(Decimal(str(float(raw))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return p, "strong" if p >= t["strong"] else "weak" if p >= t["weak"] else None


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(text) if p and p.strip()]
    return [p for p in parts if len(p) > 1]


def load_examples(path: Path = TRAINING) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def load_thresholds(path: Path = THRESHOLDS) -> dict[str, dict[str, float]]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["risk"]


class RiskClassifier:
    def __init__(
        self,
        embedder: Embedder,
        examples: list[dict] | None = None,
        thresholds: dict[str, dict[str, float]] | None = None,
        model_name: str = "",
    ):
        self.embedder = embedder
        self.examples = examples if examples is not None else load_examples()
        self.thresholds = thresholds if thresholds is not None else load_thresholds()
        x = self._embed_examples(model_name)
        self._x = x
        self._last: tuple[str, list[str], np.ndarray] | None = None
        self.heads: dict[str, LogisticRegression | None] = {}
        for kind in HEADS:
            y = np.array([kind in e["risks"] for e in self.examples], dtype=int)
            if y.min() == y.max():  # no positives (or no negatives): nothing to learn
                log.warning("risk has no training examples", extra={"risk": kind})
                self.heads[kind] = None
                continue
            # class_weight=balanced: few positives per risk; C=4: light regularisation.
            self.heads[kind] = LogisticRegression(
                C=4.0, class_weight="balanced", max_iter=2000
            ).fit(x, y)
        self.category_head = LogisticRegression(C=4.0, max_iter=2000).fit(
            x, [e["category"] for e in self.examples]
        )
        log.info(
            "risk classifier trained",
            extra={"examples": len(self.examples), "risks": len(RISKS), "model": model_name},
        )

    def _embed_examples(self, model_name: str) -> np.ndarray:
        """Embeddings of the training texts, cached on disk by content + model."""
        texts = [e["text"] for e in self.examples]
        key = hashlib.sha256(("\n".join(texts) + model_name).encode()).hexdigest()[:16]
        path = CACHE / f"risk_examples_{key}.npy"
        if model_name and path.exists():
            return np.load(path)
        vecs = self.embedder.embed_queries(texts)
        if model_name:
            CACHE.mkdir(exist_ok=True)
            np.save(path, vecs)
        return vecs

    def warm_up(self) -> None:
        """Run one realistic ticket through bge-m3 and every head at startup, so the first real
        ticket doesn't pay the model's first-call cost (~1.6 s measured)."""
        text = "Hi, what is the minimum deposit? Also, should I buy gold now?"
        self.injection(text)
        self.predict(text)
        self._last = None

    def _vectors(self, text: str) -> tuple[list[str], np.ndarray]:
        """Whole ticket + each sentence, embedded once per ticket (the injection gate and the
        risk node read the same ticket one after the other)."""
        if self._last is None or self._last[0] != text:
            units = sentences(text)
            texts = [text, *units] if len(units) > 1 else [text]
            self._last = (text, texts, self.embedder.embed_queries(texts))
        return self._last[1], self._last[2]

    def injection(self, text: str) -> InjectionScore:
        head = self.heads[INJECTION]
        if head is None:
            return InjectionScore(0.0, None, -1)
        texts, vecs = self._vectors(text)
        probs = head.predict_proba(vecs)[:, 1]
        i = int(np.argmax(probs))
        p, strength = score(probs[i], self.thresholds.get(INJECTION, {"weak": 0.5, "strong": 0.8}))
        return InjectionScore(p, strength, i - 1 if len(texts) > 1 else -1)

    def predict(self, text: str) -> RiskResult:
        texts, vecs = self._vectors(text)
        result = RiskResult()

        for kind in RISKS:
            head = self.heads[kind]
            if head is None:
                result.probabilities[kind] = 0.0
                continue
            probs = head.predict_proba(vecs)[:, 1]
            i = int(np.argmax(probs))
            p, strength = score(probs[i], self.thresholds.get(kind, {"weak": 0.5, "strong": 0.8}))
            result.probabilities[kind] = p
            if strength:
                result.signals.append(
                    RiskSignal(
                        kind=kind,
                        probability=p,
                        strength=strength,
                        method="classifier",
                        unit=i - 1 if len(texts) > 1 else -1,
                        nearest_example=self._nearest(kind, vecs[i]),
                    )
                )

        cat = self.category_head.predict_proba(vecs[:1])[0]
        order = np.argsort(cat)[::-1]
        result.category = str(self.category_head.classes_[order[0]])
        result.category_probability = round(float(cat[order[0]]), 3)
        result.category_margin = round(float(cat[order[0]] - cat[order[1]]), 3)
        return result

    def _nearest(self, kind: str, v: np.ndarray) -> str:
        idx = [i for i, e in enumerate(self.examples) if kind in e["risks"]]
        sims = self._x[idx] @ v
        return self.examples[idx[int(np.argmax(sims))]]["text"]

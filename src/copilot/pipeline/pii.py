"""PII masking (PRD FR-1.3) with Microsoft Presidio: typed placeholders before any LLM call.

    "card 4111 1111 1111 1111, أنا محمد علي"  →  "card [CARD_1], أنا [NAME_1]"

Two kinds of recognizers, each used for what it is best at:
- **Patterns** (deterministic, checksum-validated) for structured identifiers, on every language:
  credit cards (Luhn), IBAN (checksum), email, phone (Google's phonenumbers library, Egypt/Gulf
  regions added), plus OUR recognizers for document and account numbers, which only count when
  a context word is near ("passport", "account", "جواز", "حساب"), so "10 USD" is never masked.
- **NER** (a model) for names and addresses, which no pattern can find. Setting `PII_NER`:
  - `gliner` (default in .env): GLiNER-PII (`urchade/gliner_multi_pii-v1`), a small multilingual
    model plugged into Presidio as a recognizer. Finds names in Arabic, Arabizi, Russian,
    Vietnamese... and addresses. ~80 ms per ticket on CPU.
  - `spacy`: Presidio's default English spaCy NER, names only, and only on text that looks
    English (it mislabels ordinary words in other languages as names). Fast; used by unit tests.

Replacement is done here: overlapping spans are resolved (strongest wins), placeholders are
numbered in reading order, and the same value gets the same placeholder within one ticket.

The placeholder → value mapping is returned to the caller only: never logged, never in the graph
state, never sent to the LLM or LangSmith. It is persisted encrypted by pii_vault.py.
"""

import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from presidio_analyzer import (
    AnalyzerEngine,
    LocalRecognizer,
    Pattern,
    PatternRecognizer,
    RecognizerResult,
)
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import PhoneRecognizer

NerMode = Literal["gliner", "spacy"]

# Presidio entity → our placeholder type (closed list: a new entity needs a decision here).
PLACEHOLDER = {
    "CREDIT_CARD": "CARD",
    "IBAN_CODE": "ACCOUNT",
    "ACCOUNT_NUMBER": "ACCOUNT",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "DOCUMENT_NUMBER": "DOC",
    "PERSON": "NAME",
    "ADDRESS": "ADDRESS",
}


def _words(text: str) -> frozenset[str]:
    """Word lists are written as plain text (easier to read and extend than quoted lists)."""
    return frozenset(text.split())


# GLiNER is zero-shot: it is asked for ONLY these labels, in plain words. IDs (cards, IBAN,
# phones, documents, accounts) stay with the deterministic patterns, which are more reliable.
GLINER_LABELS = {"person": "PERSON", "address": "ADDRESS"}
# Words GLiNER tags as "person" that are not names: pronouns, relatives, roles. A span made only
# of these (plus my/the/this...) is dropped. Measured on the eval set (2026-11-23).
_NOT_NAMES = _words(
    "i me you he she we they him her them my your our his their this that the a an "
    "dad mom mum father mother brother sister cousin wife husband son daughter friend "
    "customer client agent support team people sir madam "
    "أنا انا أنت انت إنت انتي إنتي أنتم انتو انتوا هو هي نحن احنا "
    "я ты вы он она мы они "
    "tôi bạn anh chị em"
)
_DIGITS = frozenset("0123456789٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹")

SCORE_THRESHOLD = 0.5  # Presidio confidence to mask; context words lift weak patterns over it
# Per-entity exceptions. A phone number is already validated by Google's phonenumbers library
# (0.4 without a "phone" word nearby), so it does not need a context boost.
MIN_SCORE = {"PHONE_NUMBER": 0.4}
# Domain words an NER model may mistake for names. Presidio's allow_list: never masked.
# Add a word here when an eval run shows a false positive.
ALLOW_LIST = [
    "Max", "Chargeback", "Valetax", "Gold", "Silver", "Islamic", "MT4", "MT5",
    "Visa", "Mastercard", "Skrill", "Neteller", "USDT", "Bitcoin",
]  # fmt: skip
PHONE_REGIONS = ("EG", "AE", "SA", "KW", "QA", "BH", "OM", "JO", "GB", "US", "DE", "IN")
# Common English words: in spaCy mode, names are looked for only in text that is clearly English.
_EN_WORDS = _words(
    "the a an is are was i my me you your we our it to of in on for and or but with have has "
    "had do does did can could would should will what how why when where which who this that "
    "not no please thanks hi hello"
)


def _document_recognizer() -> PatternRecognizer:
    # Passport / national ID shapes: 1–2 letters + 6–9 digits, or 8–14 digits. Weak on their own
    # (score 0.2), masked only with a context word nearby (Presidio adds ~0.35 for context).
    return PatternRecognizer(
        name="DocumentNumberRecognizer",
        supported_entity="DOCUMENT_NUMBER",
        patterns=[
            Pattern("letters+digits", r"\b[A-Z]{1,2}\d{6,9}\b", 0.2),
            Pattern("digits", r"\b\d{8,14}\b", 0.2),
        ],
        context=["passport", "national", "id", "document", "license", "جواز", "هوية", "بطاقة"],
    )


def _account_recognizer() -> PatternRecognizer:
    # Trading-account / MT4 / MT5 login numbers: 5–12 digits next to an account word.
    return PatternRecognizer(
        name="AccountNumberRecognizer",
        supported_entity="ACCOUNT_NUMBER",
        patterns=[Pattern("account", r"\b\d{5,12}\b", 0.2)],
        context=["account", "acct", "login", "mt4", "mt5", "حساب", "حسابي"],
    )


SURE_NAME = 0.8  # GLiNER score above which a span is a name without further evidence


def _looks_like_name(text: str, start: int, end: int, score: float) -> bool:
    """Measured on the eval set: real names score 0.93+ in Arabic/Russian/Vietnamese, while
    non-names score 0.5–0.87 ("friend", "عايز اسحب فلوسي"). Below SURE_NAME the only real names
    were capitalised mid-sentence Latin names ("agent Sam" 0.66, "Mohamed Samir" 0.56)."""
    span = text[start:end]
    words = [w.strip(".,!?;:'\"()").lower() for w in span.split()]
    if all(w in _NOT_NAMES for w in words):
        return False  # "I", "my dad", "you people", "أنت"
    if score >= SURE_NAME:
        return True
    # Weaker spans need a capitalised word that is not just capitalised because it starts a
    # sentence ("Bên bạn ...", a one-word subject line "Advisor").
    at_sentence_start = not text[:start].strip() or text[:start].rstrip()[-1] in ".!?\n:>"
    candidates = span.split()[1:] if at_sentence_start else span.split()
    return any(w[:1].isupper() for w in candidates)


class MultilingualNameRecognizer(LocalRecognizer):
    """Names and street addresses in any language with GLiNER-PII, as a Presidio recognizer.

    Rules on top of the model (it is a model: scores alone don't separate "friend" 0.87 from
    "Mohamed Samir" 0.56, so lexical rules do):
    - a person must look like a name (`_looks_like_name`);
    - an address must contain a number: a street address is PII, a city or country alone
      ("I live in New York") is not, and the compliance check needs to see it.
    """

    def __init__(self, model_name: str, threshold: float):
        self.model_name, self.threshold = model_name, threshold
        super().__init__(
            supported_entities=list(GLINER_LABELS.values()),
            name="MultilingualNameRecognizer",
            supported_language="en",  # Presidio's language key; the model itself is multilingual
        )

    def load(self) -> None:
        from gliner import GLiNER  # noqa: PLC0415 (loads torch + the model only when GLiNER is on)

        self.model = GLiNER.from_pretrained(self.model_name, map_location="cpu")

    def analyze(self, text, entities, nlp_artifacts=None) -> list[RecognizerResult]:
        results = []
        for e in self.model.predict_entities(text, list(GLINER_LABELS), threshold=self.threshold):
            entity, span = GLINER_LABELS[e["label"]], e["text"]
            if entity not in entities:
                continue
            if entity == "PERSON" and not _looks_like_name(text, e["start"], e["end"], e["score"]):
                continue
            if entity == "ADDRESS" and not _DIGITS & set(span):
                continue
            results.append(RecognizerResult(entity, e["start"], e["end"], float(e["score"])))
        return results


@lru_cache
def _analyzer(ner: NerMode, gliner_model: str, gliner_threshold: float) -> AnalyzerEngine:
    """Built once per process and configuration (spaCy ~0.3 s; GLiNER ~10 s, then cached)."""
    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",  # always needed: tokens + lemmas for context words
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    analyzer.registry.remove_recognizer("PhoneRecognizer")
    analyzer.registry.add_recognizer(PhoneRecognizer(supported_regions=PHONE_REGIONS))
    analyzer.registry.add_recognizer(_document_recognizer())
    analyzer.registry.add_recognizer(_account_recognizer())
    if ner == "gliner":
        # GLiNER replaces spaCy for names: one name source, multilingual.
        analyzer.registry.remove_recognizer("SpacyRecognizer")
        analyzer.registry.add_recognizer(
            MultilingualNameRecognizer(gliner_model, gliner_threshold)
        )
    return analyzer


@dataclass
class Masked:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder → original value

    @property
    def types(self) -> list[str]:
        return sorted({p.strip("[]").rsplit("_", 1)[0].lower() for p in self.mapping})


@dataclass(frozen=True)
class Detection:
    """One masked span, for monitoring. Deliberately has NO value field: safe to log and trace."""

    field: str  # subject | body
    placeholder: str  # [CARD_1]
    type: str  # card
    recognizer: str  # CreditCardRecognizer, MultilingualNameRecognizer, ...
    score: float
    length: int  # characters masked


@dataclass
class MaskReport:
    masked: dict[str, str]  # field → masked text (what the graph, LLM and LangSmith see)
    mapping: dict[str, str]  # placeholder → real value: ONLY for the vault, never logged
    detections: list[Detection]
    residual: list[str]  # kinds of PII-looking text LEFT after masking (possible misses)
    ms: float

    @property
    def types(self) -> list[str]:
        return sorted({d.type for d in self.detections})

    def summary(self) -> dict:
        """Counts only: what logs, trace metadata and the API response may carry."""
        by_type: dict[str, int] = {}
        by_recognizer: dict[str, int] = {}
        for d in self.detections:
            by_type[d.type] = by_type.get(d.type, 0) + 1
            by_recognizer[d.recognizer] = by_recognizer.get(d.recognizer, 0) + 1
        return {
            "pii_types": self.types,
            "pii_count": len(self.detections),
            "pii_by_type": by_type,
            "pii_by_recognizer": by_recognizer,
            "pii_residual_suspect": self.residual,
            "pii_ms": round(self.ms, 1),
        }


# Residual check: after masking, text that still LOOKS like PII is a possible miss.
# Placeholders are removed first. Kinds are reported, never the matched text.
_PLACEHOLDER_RE = re.compile(r"\[[A-Z]+_\d+\]")
_RESIDUAL = {
    "long_number": re.compile(r"\d(?:[ .\-]?\d){7,}"),  # 8+ digits: card, phone, ID, IBAN body
    "email_like": re.compile(r"\S+@\S+\.\w{2,}"),
    "iban_like": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{10,}"),
}


def residual_suspects(masked_text: str) -> list[str]:
    text = _PLACEHOLDER_RE.sub(" ", masked_text)
    return sorted(kind for kind, rx in _RESIDUAL.items() if rx.search(text))


def _drop_overlaps(results: list[RecognizerResult]) -> list[RecognizerResult]:
    """Keep the strongest span where two overlap (e.g. a card number also matching 'digits')."""
    kept: list[RecognizerResult] = []
    for r in sorted(results, key=lambda r: (-r.score, -(r.end - r.start))):
        if all(r.end <= k.start or r.start >= k.end for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: r.start)


def _looks_english(text: str) -> bool:
    """spaCy mode only: at least 2 common English words making up 15%+ of the words."""
    words = [w.strip(".,!?;:'\"()").lower() for w in text.split()]
    hits = sum(w in _EN_WORDS for w in words)
    return hits >= 2 and hits / max(len(words), 1) >= 0.15


@dataclass(frozen=True)
class Masker:
    ner: NerMode
    gliner_model: str = ""
    gliner_threshold: float = 0.5

    @classmethod
    def from_settings(cls, st) -> "Masker":
        return cls(st.pii_ner, st.pii_gliner_model, st.pii_gliner_threshold)

    def warm_up(self) -> None:
        """Load the models now (GLiNER ~10 s) so the first ticket doesn't pay for it."""
        self.mask_ticket(text="warm up")

    def mask(self, text: str) -> Masked:
        r = self.mask_ticket(text=text)
        return Masked(r.masked["text"], r.mapping)

    def mask_fields(self, **fields: str) -> tuple[dict[str, str], dict[str, str]]:
        r = self.mask_ticket(**fields)
        return r.masked, r.mapping

    def mask_ticket(self, **fields: str) -> MaskReport:
        """Mask several fields of ONE ticket (subject, body) with shared numbering, so a
        placeholder means the same value everywhere in the ticket and in the vault.
        Returns the masked text, the mapping (vault only) and monitoring data (no values)."""
        t0 = time.perf_counter()
        by_value: dict[tuple[str, str], str] = {}  # (type, value) → placeholder
        counts: dict[str, int] = {}
        detections: list[Detection] = []
        masked = {
            name: self._mask_one(name, text, by_value, counts, detections)
            for name, text in fields.items()
        }
        residual = sorted({k for text in masked.values() for k in residual_suspects(text)})
        return MaskReport(
            masked=masked,
            mapping={ph: value for (_, value), ph in by_value.items()},
            detections=detections,
            residual=residual,
            ms=(time.perf_counter() - t0) * 1000,
        )

    def _mask_one(
        self, name: str, text: str, by_value: dict, counts: dict, detections: list
    ) -> str:
        if not text.strip():
            return text
        entities = [
            e
            for e in PLACEHOLDER
            if e not in ("PERSON", "ADDRESS") or self.ner == "gliner" or _looks_english(text)
        ]
        found = _analyzer(self.ner, self.gliner_model, self.gliner_threshold).analyze(
            text=text,
            language="en",  # recognizers are registered as "en"; patterns work on any script
            entities=entities,
            allow_list=ALLOW_LIST,
            score_threshold=min(MIN_SCORE.values()),
        )
        found = [r for r in found if r.score >= MIN_SCORE.get(r.entity_type, SCORE_THRESHOLD)]
        found = _drop_overlaps(found)

        # Number placeholders in reading order; the same value always gets the same placeholder.
        for r in found:
            kind, value = PLACEHOLDER[r.entity_type], text[r.start : r.end]
            if (kind, value) not in by_value:
                counts[kind] = counts.get(kind, 0) + 1
                by_value[(kind, value)] = f"[{kind}_{counts[kind]}]"
            detections.append(
                Detection(
                    field=name,
                    placeholder=by_value[(kind, value)],
                    type=kind.lower(),
                    recognizer=(r.recognition_metadata or {}).get("recognizer_name", "?"),
                    score=round(r.score, 2),
                    length=len(value),
                )
            )

        # Replace from the end so earlier offsets stay valid.
        out = text
        for r in reversed(found):
            ph = by_value[(PLACEHOLDER[r.entity_type], text[r.start : r.end])]
            out = out[: r.start] + ph + out[r.end :]
        return out

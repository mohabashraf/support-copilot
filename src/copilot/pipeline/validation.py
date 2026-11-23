"""After the two classification calls: validate, merge, and gate on confidence. Code, no LLM.

    classify_safety ─┐
                     ├─► validate_classification ─► confidence_gate ─► policy_gate
    classify_triage ─┘

Three layers, each with its own job:

1. `check_triage`: rules on ONE answer that Pydantic can't express (the schema sent to the API
   stays flat: no regex, no numeric bounds). A broken answer raises LLMOutputError → the
   node's RetryPolicy asks again; never "fixed" silently.
2. `merge`: the two answers become ONE `Classification` (intents and flags from safety, the
   rest from triage). Contradictions are resolved toward the stricter value and every
   correction is recorded in `fixes` (shown to the agent, counted in monitoring).
3. `confidence`: the model's self-reported confidence is never trusted alone (models are
   overconfident, PRD FR-5.1). Deterministic signals back it up; ANY signal → `low_confidence`.
   `low_resource_language` is decided here too: it is about how much we trust the model's work.
"""

import math
import re
from dataclasses import dataclass, field

from copilot.pipeline.llm import LLMOutputError
from copilot.pipeline.risk_classifier import load_category_rules
from copilot.pipeline.schemas import Category, Classification, Intent, SafetyOut, TriageOut

# Strictest first: the ticket's category is its most restrictive intent (PRD FR-2.1d).
RESTRICTIVENESS: list[Category] = [
    "aml_compliance",
    "complaint",
    "trading_advice_request",
    "verification",
    "withdrawal",
    "deposit",
    "account_opening",
    "other",
]
_RANK = {c: i for i, c in enumerate(RESTRICTIVENESS)}
# decision_requested fixes the topic (prompt rule; enforced here, not trusted)
DECISION_TOPIC: dict[str, Category] = {
    "kyc": "verification",
    "complaint": "complaint",
    "aml": "aml_compliance",
}
# ISO 639-1/639-3 + optional region; ckb and zh-TW are valid
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")
_PLACEHOLDER = re.compile(r"\[[A-Z_]+_\d+\]")
# Languages written in each script (a cheap, deterministic check of the model's language code)
_SCRIPT_LANGS = {
    "arabic": {"ar", "fa", "ur", "ckb", "ps"},
    "cyrillic": {"ru", "uk", "bg", "sr", "kk", "be", "mk", "ky", "tg", "mn"},
    "cjk": {"zh", "zh-TW", "zh-CN", "ja", "ko"},
}
# The local classifier's category must be this sure before its disagreement counts as a signal.
# From classifier/thresholds.toml [category], measured by `copilot eval risks` (see the comment
# there). Its 'other' never counts: it means "nothing specific found", not a different topic.
CLASSIFIER_SURE_P = load_category_rules()["sure_probability"]


# ---- 1. one answer ---------------------------------------------------------------------------


def check_triage(out: TriageOut) -> TriageOut:
    """Business rules on the triage answer. Raises LLMOutputError (→ retry, then a person)."""
    for name in ("language", "reply_language"):
        if not _LANG.match(getattr(out, name)):
            raise LLMOutputError(f"classify_triage: {name}={getattr(out, name)!r} is not a code")
    if not math.isfinite(out.confidence):
        raise LLMOutputError("classify_triage: confidence is not a number")
    return out


# ---- 2. merge --------------------------------------------------------------------------------


def strictest(categories) -> Category:
    return min(categories, key=_RANK.__getitem__, default="other")


def merge(safety: dict, triage: dict) -> tuple[Classification, list[str]]:
    """safety (SafetyOut dump) + triage (TriageOut dump) → one Classification + fixes."""
    fixes: list[str] = []
    s, t = SafetyOut.model_validate(safety), TriageOut.model_validate(triage)

    intents = []
    for i, it in enumerate(s.intents):
        want = DECISION_TOPIC.get(it.decision_requested)
        if want and it.topic != want:
            fixes.append(f"intent{i}.topic {it.topic}→{want} (decision={it.decision_requested})")
            it = Intent(topic=want, decision_requested=it.decision_requested, text=it.text)
        intents.append(it)

    topics = [it.topic for it in intents]
    category = strictest([t.category, *topics])
    if category != t.category:
        fixes.append(f"category {t.category}→{category} (strictest intent)")
    elif t.category not in topics and set(topics) != {"other"}:
        # triage is stricter than safety: kept, but they disagree
        fixes.append(f"category {t.category} kept (safety topics: {', '.join(topics)})")

    urgency = t.urgency
    if s.vulnerable_customer and urgency != "critical":  # PRD FR-4.7
        fixes.append(f"urgency {urgency}→critical (vulnerable customer)")
        urgency = "critical"

    query = " ".join(_PLACEHOLDER.sub(" ", t.search_query).split())
    if query != t.search_query.strip():
        fixes.append("search_query: placeholders removed")

    confidence = min(max(t.confidence, 0.0), 1.0)
    if confidence != t.confidence:
        fixes.append(f"confidence {t.confidence}→{confidence} (clamped)")

    c = Classification(
        category=category,
        intents=intents,
        urgency=urgency,
        language=t.language,
        reply_language=t.reply_language or t.language,
        sentiment=t.sentiment,
        needs_account_action=t.needs_account_action,
        vulnerable_customer=s.vulnerable_customer,
        security_sensitive=s.security_sensitive,
        search_query=query,
        confidence=confidence,
    )
    return c, fixes


# ---- 3. confidence ---------------------------------------------------------------------------


@dataclass
class ConfidenceResult:
    self_reported: float
    threshold: float
    signals: list[str] = field(default_factory=list)  # evidence, never the customer's text
    reasons: list[str] = field(default_factory=list)  # ReasonCode values

    @property
    def passed(self) -> bool:
        return "low_confidence" not in self.reasons


def script_of(text: str) -> str | None:
    """Dominant non-Latin script of the text (letters only), or 'latin', or None if no letters."""
    counts = {"arabic": 0, "cyrillic": 0, "cjk": 0, "latin": 0}
    for ch in text:
        o = ord(ch)
        if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFEFF:
            counts["arabic"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
            counts["cjk"] += 1
        elif ch.isascii() and ch.isalpha():
            counts["latin"] += 1
    total = sum(counts.values())
    if not total:
        return None
    best = max(counts, key=counts.get)
    return best if counts[best] / total >= 0.6 else None


def confidence(
    c: Classification,
    *,
    text: str,
    risk: dict | None,
    unquoted_intents: list[int],
    fixes: list[str],
    threshold: float,
    low_resource_languages: list[str],
) -> ConfidenceResult:
    r = ConfidenceResult(self_reported=c.confidence, threshold=threshold)

    if c.confidence < threshold:
        r.signals.append(f"self_reported:{c.confidence:.2f}<{threshold}")
    if unquoted_intents:  # the safety model quoted words that are not in the ticket
        r.signals.append(f"safety:unquoted_intents={unquoted_intents}")
    # the two calls disagree on the topic (merge had to correct one of them)
    # 'other' = "nothing specific" (e.g. a thank-you), not a different topic: never counts
    disagree = [f for f in fixes if (f.startswith("category ") or ".topic " in f)
                and not f.startswith("category other→")]  # fmt: skip
    if disagree:
        r.signals.append("llm_disagreement:" + "; ".join(disagree))
    # the local classifier, when SURE, disagrees with every topic the LLMs found
    if risk and (cat := risk.get("category")) and cat != "other":
        sure = risk.get("category_probability", 0) >= CLASSIFIER_SURE_P
        topics = {c.category, *(i.topic for i in c.intents)}
        if sure and cat not in topics:
            r.signals.append(f"classifier_disagrees:{cat} p={risk['category_probability']}")
    # the language code doesn't fit the script the ticket is written in
    script = script_of(text)
    if script and script != "latin" and c.language not in _SCRIPT_LANGS[script]:
        r.signals.append(f"language_script_mismatch:{c.language} vs {script}")
    # Latin script is fine for Arabizi / transliterated Russian, never for Chinese/Japanese/Korean
    if script == "latin" and c.language in _SCRIPT_LANGS["cjk"]:
        r.signals.append(f"language_script_mismatch:{c.language} vs latin")

    if r.signals:
        r.reasons.append("low_confidence")
    if c.language in low_resource_languages or c.reply_language in low_resource_languages:
        r.reasons.append("low_resource_language")
    return r

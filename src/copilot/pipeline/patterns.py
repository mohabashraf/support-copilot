"""Exact checks that run FIRST, before any model or LLM sees the ticket (node `pattern_check`).

- Prompt injection: literal instruction-override strings. If one matches, the LLM nodes use a
  hardened prompt and the ticket gets NO AI draft: a person replies (reason prompt_injection).
- Account security: literal requests to change contact details or switch off 2FA.
- Spam / empty: no letters (only emojis or symbols), one character repeated, mass links or
  promotional phrases. These skip every model and LLM call and go to a person at low priority
  (reason spam_or_empty). Nothing is ever dropped: a real customer can look like spam.

Exact patterns are certain when they match (always "strong"); they know only these phrases. The
risk classifier (meaning, any language) runs after them for everything else.
Evidence is the pattern's NAME, never the customer's text, so it is safe for monitoring.
"""

import re
from dataclasses import dataclass

# name → compiled pattern. Names are what monitoring and the audit log show.
INJECTION = {
    "ignore_previous_instructions": r"\b(ignore|disregard|forget)\b.{0,20}\b(previous|prior|above|all)\b.{0,20}\b(instructions|rules|prompts?)\b",  # noqa: E501
    "system_note_to_ai": r"\b(system|admin)\s*(note|prompt|message|instruction)s?\b\s*(to (the )?ai)?\s*:",  # noqa: E501
    "mode_switch": r"\byou are now (in )?(an? )?(admin|developer|god|unrestricted|jailbreak)",
    "output_schema_spoof": r'"(must_route|category|draft|routing)"\s*:',
    "ar_ignore_instructions": r"(تجاهل|انسى|انس)\s*(كل|جميع)?\s*(التعليمات|القواعد|الأوامر)",
}
SECURITY = {
    "disable_2fa": r"\b(disable|turn off|remove|deactivate)\b.{0,15}\b(2fa|two[- ]factor|otp)\b",
    "change_contact_details": r"\b(change|update|replace)\b.{0,20}\b(registered |account )?(email|e-mail|phone( number)?|mobile)\b",  # noqa: E501
}
PROMO = r"\b(click here|earn \$?\d+|guaranteed profit|free money|casino|bet now|buy followers|crypto signals group|whatsapp me)\b"  # noqa: E501
_URL = re.compile(r"https?://|www\.", re.I)

_COMPILED = {
    "prompt_injection": {n: re.compile(p, re.I) for n, p in INJECTION.items()},
    "security_sensitive": {n: re.compile(p, re.I) for n, p in SECURITY.items()},
}
_PROMO = re.compile(PROMO, re.I)
_PLACEHOLDER = re.compile(r"\[[A-Z]+_\d+\]")


@dataclass(frozen=True)
class PatternHit:
    kind: str  # prompt_injection | security_sensitive
    pattern: str  # the pattern's name (evidence), never the customer's text


@dataclass(frozen=True)
class PatternResult:
    hits: tuple[PatternHit, ...]
    spam_reason: str | None  # empty | no_letters | repeated_characters | links | promotional

    @property
    def injection(self) -> bool:
        return any(h.kind == "prompt_injection" for h in self.hits)

    @property
    def spam(self) -> bool:
        return self.spam_reason is not None


def spam_reason(text: str) -> str | None:
    if not text.strip():
        return "empty"
    t = _PLACEHOLDER.sub(" ", text).strip()
    if not t:
        return None  # only masked details (card, phone…): a real customer sharing data, not spam
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 2:
        return "no_letters"  # emojis, punctuation, digits only
    if len(t) >= 8 and max(t.count(c) for c in set(t)) / len(t) > 0.8:
        return "repeated_characters"
    if len(_URL.findall(t)) >= 3:
        return "links"
    if _PROMO.search(t):
        return "promotional"
    return None


def check(text: str) -> PatternResult:
    hits = [
        PatternHit(kind, name)
        for kind, patterns in _COMPILED.items()
        for name, rx in patterns.items()
        if rx.search(text)
    ]
    return PatternResult(tuple(hits), spam_reason(text))

"""post_check: deterministic checks on the draft AFTER it is written (PRD FR-4.3, FR-4.4).

The prompt asks the drafter to follow the rules; this module checks that it did. Code, no LLM,
so it cannot be talked out of a rule. Each failure is a short code + detail (numbers and
pattern names only, never the customer's text):

    unsupported_number   a number in the reply that is in no CITED chunk ("8 USD" ok if the
                         cited FAQ says 8 USD). Digits normalised first: Arabic-Indic ٠–٩,
                         Persian ۰–۹, Thai and every other Unicode decimal digit → 0–9, and
                         thousands separators removed ("1,000" = "1000").
    advice_language      a recommendation verb near a trading word ("we recommend buying",
                         "ننصحك بشراء"), or a prediction ("gold will go up")
    promise              a guarantee or a claim about this customer's account ("your account
                         has been approved", "guaranteed", "تمت الموافقة")
    unknown_citation     a cited ID that was not in the sources (also checked in the draft node)
    unknown_placeholder  a placeholder ([NAME_2]) that is not in the ticket: the drafter made up
                         personal data
    wrong_language       the reply's script doesn't match reply_language (Arabic reply in Latin
                         letters, or the reverse)

A failed draft gets ONE redraft with the failures as feedback; a second failure → a person
(post_check_failed) and the failed text is never shown as the reply.
"""

import re
import unicodedata

from copilot.pipeline.schemas import DraftOut
from copilot.pipeline.validation import script_of

_PLACEHOLDER = re.compile(r"\[[A-Z_]+_\d+\]")
_THOUSANDS = re.compile(r"(?<=\d)[,٬،](?=\d{3}\b)")  # 1,000 · 1٬000
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Trading ACTIONS (not the word "trading": "verify before trading" is a normal support sentence)
_ACT_EN = (r"(buy|buying|sell|selling|go long|go short|open (a |an )?(buy|sell|long|short)?\s?"
           r"(position|trade)|use (a |higher |lower |more )?leverage|(increase|raise|lower) your "
           r"leverage|close your (position|trade)|invest in)")  # fmt: skip
_ASSET = r"(price|market|gold|oil|silver|bitcoin|btc|eth|eur\S*|usd\S*|gbp\S*|pair|stock|index)"
_ADVICE_EN = [
    ("recommend_trade", rf"\b(recommend|suggest|advise)\w*\b.{{0,40}}\b{_ACT_EN}"),
    ("should_trade", rf"\byou (should|must|need to|ought to)\s+(\w+\s+){{0,2}}{_ACT_EN}"),
    ("good_time", r"\b(good|best|right|great) (time|moment|idea|opportunity) to "
                  r"(buy|sell|trade|invest)"),
    ("prediction", rf"\b{_ASSET}\s+(will|is going to|is likely to)\s+(go up|go down|rise|fall|"
                   r"drop|increase|decrease|reach)"),
    ("best_choice", r"\bthe best (pair|asset|leverage|strategy|time to)\b"),
]  # fmt: skip
_ACT_AR = r"(شراء|اشتر|بيع|فتح صفقة|صفقة شراء|صفقة بيع|الرافعة|رافعة)"
_ADVICE_AR = [
    ("recommend_trade_ar", rf"(ننصح|أنصح|انصح|نوصي|أوصي|نقترح|أقترح)\S*.{{0,40}}{_ACT_AR}"),
    ("should_trade_ar", rf"(يجب|ينبغي|عليك)\s+(أن\s+)?(\S+\s+)?{_ACT_AR}"),
    ("prediction_ar", r"(سيرتفع|سينخفض|سوف يرتفع|سوف ينخفض)"),
]  # fmt: skip
_NOT = r"(?<!not )(?<!cannot )(?<!can't )(?<!can’t )(?<!unable to )"
_PROMISE = [
    ("guarantee", rf"\b{_NOT}(guarantee|guaranteed|promise|we assure)\w*"),
    ("account_claim", r"\byour (account|verification|withdrawal|deposit|request|refund|documents?)"
                      r" (has|have) been (approved|verified|unfrozen|processed|completed|accepted|"
                      r"sent|credited|released)"),  # fmt: skip
    ("will_be_approved", r"\bwill (definitely |certainly )?be "
                         r"(approved|verified|unfrozen|refunded|released|accepted)"),  # fmt: skip
    ("guarantee_ar", r"(?<!لا )(نضمن|مضمون|نعدك|نتعهد)"),
    ("account_claim_ar",
     r"(تمت الموافقة|تم توثيق حسابك|تم فك التجميد|تم تحويل المبلغ|تم قبول)"),
]  # fmt: skip
_PATTERNS = [
    (n, re.compile(p, re.I | re.S), "advice_language") for n, p in _ADVICE_EN + _ADVICE_AR
]
_PATTERNS += [(n, re.compile(p, re.I | re.S), "promise") for n, p in _PROMISE]
_ARABIC_SCRIPT = {"ar", "fa", "ur", "ckb", "ps"}


def normalise_digits(text: str) -> str:
    """Every Unicode decimal digit → ASCII 0–9; thousands separators between digits removed."""
    out = "".join(
        str(unicodedata.decimal(ch)) if ch.isdigit() and unicodedata.decimal(ch, None) is not None
        else ch
        for ch in text
    )  # fmt: skip
    return _THOUSANDS.sub("", out)


def numbers(text: str) -> set[str]:
    """Numbers in a text, placeholders ([CARD_1]) removed first. '1:2000' → {'1', '2000'};
    '8.00' → '8' (trailing zeros don't make a different number)."""
    clean = _PLACEHOLDER.sub(" ", normalise_digits(text))
    out = set()
    for n in _NUMBER.findall(clean):
        out.add(n.rstrip("0").rstrip(".") if "." in n else n.lstrip("0") or "0")
    return out


def check(
    draft: DraftOut, sources: list[dict], ticket_text: str, reply_language: str
) -> list[str]:
    if draft.abstain or not draft.reply.strip():
        return []
    reply, fails = draft.reply, []
    by_id = {s["id"]: s for s in sources}

    unknown = [c for c in draft.cited_chunk_ids if c not in by_id]
    if unknown:
        fails.append(f"unknown_citation:{','.join(unknown)}")
    cited_text = " ".join(f"{by_id[c]['section']} {by_id[c]['text']}"
                          for c in draft.cited_chunk_ids if c in by_id)  # fmt: skip
    missing = sorted(numbers(reply) - numbers(cited_text), key=lambda n: (len(n), n))
    if missing:
        fails.append(f"unsupported_number:{','.join(missing)}")

    for name, rx, code in _PATTERNS:
        if rx.search(reply):
            fails.append(f"{code}:{name}")

    extra = set(_PLACEHOLDER.findall(reply)) - set(_PLACEHOLDER.findall(ticket_text))
    if extra:
        fails.append(f"unknown_placeholder:{','.join(sorted(extra))}")

    script = script_of(_PLACEHOLDER.sub(" ", reply))
    want_arabic = reply_language.split("-")[0] in _ARABIC_SCRIPT
    if script and (
        (want_arabic and script != "arabic") or (not want_arabic and script == "arabic")
    ):
        fails.append(f"wrong_language:{script} for {reply_language}")
    return fails


def feedback(failures: list[str]) -> str:
    """What the drafter is told on the redraft (codes only, no customer text)."""
    hints = {
        "unsupported_number": "remove every number that is not written in a source you cite "
                              "(or cite the source that contains it)",
        "advice_language": "remove anything that recommends, suggests or predicts trading",
        "promise": "remove guarantees and any statement about this customer's account status",
        "unknown_citation": "cite only the ids given in <sources>",
        "unknown_placeholder": "use only placeholders that appear in the ticket",
        "wrong_language": "write the whole reply in <reply_language>",
    }  # fmt: skip
    lines = [f"- {f}: {hints.get(f.split(':')[0], 'fix it')}" for f in failures]
    return "Your previous draft failed these checks:\n" + "\n".join(lines)

"""PII masking. New examples on purpose: the rules were tuned on the eval tickets, so testing on
them again would only prove they fit those tickets."""

import pytest

from copilot.pipeline.pii import Masker

SPACY = Masker(ner="spacy")
GLINER = Masker(ner="gliner", gliner_model="urchade/gliner_multi_pii-v1", gliner_threshold=0.5)


# ---- Patterns (deterministic, any language) -------------------------------------------------


@pytest.mark.parametrize(
    "text, placeholder, value",
    [
        ("pay with 4012 8888 8888 1881 please", "[CARD_1]", "4012 8888 8888 1881"),
        ("card 3782-822463-10005 was declined", "[CARD_1]", "3782-822463-10005"),
        ("IBAN GB82 WEST 1234 5698 7654 32", "[ACCOUNT_1]", "GB82 WEST 1234 5698 7654 32"),
        ("write to sara.k+fx@mail.co.uk", "[EMAIL_1]", "sara.k+fx@mail.co.uk"),
        ("call me on +971 50 123 4567", "[PHONE_1]", "+971 50 123 4567"),
        ("رقمي 01001234567", "[PHONE_1]", "01001234567"),
        ("my passport no. is P9876543", "[DOC_1]", "P9876543"),
        ("رقم جواز السفر A1234567", "[DOC_1]", "A1234567"),
        ("MT5 login 7712093 is blocked", "[ACCOUNT_1]", "7712093"),
        ("حسابي 8842211 مقفول", "[ACCOUNT_1]", "8842211"),
    ],
)
def test_identifiers_are_masked(text, placeholder, value):
    m = SPACY.mask(text)
    assert m.mapping == {placeholder: value}
    assert value not in m.text and placeholder in m.text


@pytest.mark.parametrize(
    "text",
    [
        "card 4111 1111 1111 1112 failed",  # fails the Luhn check: not a card
        "I paid 25000 USD yesterday",  # an amount, no account word nearby
        "my order 12345678 is late",  # no document/account word nearby
        "minimum deposit is 10 USD, withdrawals in 24 hours",
        "leverage up to 1:2000",
    ],
)
def test_numbers_that_are_not_pii_stay(text):
    assert SPACY.mask(text).text == text


def test_same_value_same_placeholder_and_reading_order():
    m = SPACY.mask("a@x.com, b@y.com, then a@x.com again")
    assert m.text == "[EMAIL_1], [EMAIL_2], then [EMAIL_1] again"
    assert m.mapping == {"[EMAIL_1]": "a@x.com", "[EMAIL_2]": "b@y.com"}


def test_empty_text():
    assert SPACY.mask("  ").mapping == {}


# ---- Names and addresses (GLiNER, multilingual) ---------------------------------------------


@pytest.mark.parametrize(
    "text, name",
    [
        ("Hello, this is Laila Mansour from Dubai.", "Laila Mansour"),
        ("اسمي خالد إبراهيم وعندي مشكلة في السحب", "خالد إبراهيم"),
        ("Здравствуйте, меня зовут Ольга Смирнова", "Ольга Смирнова"),
        ("Xin chào, tôi là Trần Thị Mai", "Trần Thị Mai"),
        ("The agent Kevin closed my ticket", "Kevin"),
    ],
)
def test_names_are_masked_in_any_language(text, name):
    m = GLINER.mask(text)
    assert name not in m.text
    assert "name" in m.types


@pytest.mark.parametrize(
    "text",
    [
        "Can my brother deposit for me?",  # relatives are not names
        "You told me it was instant!",  # pronouns are not names
        "أنت قلت إن الإيداع فوري",  # Arabic pronoun
        "I live in Germany, can I open an account?",  # a country is not PII (compliance needs it)
        "Max leverage for gold?",  # domain word (allow list)
    ],
)
def test_non_names_stay(text):
    assert GLINER.mask(text).text == text


def test_street_address_is_masked_but_city_alone_is_not():
    assert "Baker" not in GLINER.mask("I moved to 42 Baker Street, Leeds").text
    assert GLINER.mask("I moved to Leeds last year").text == "I moved to Leeds last year"


def test_fields_of_one_ticket_share_numbering():
    masked, mapping = SPACY.mask_fields(
        subject="from a@x.com", body="reply to b@y.com, cc a@x.com"
    )
    assert masked == {"subject": "from [EMAIL_1]", "body": "reply to [EMAIL_2], cc [EMAIL_1]"}
    assert mapping == {"[EMAIL_1]": "a@x.com", "[EMAIL_2]": "b@y.com"}


def test_report_has_monitoring_data_but_no_values():
    r = SPACY.mask_ticket(subject="hi", body="card 4111 1111 1111 1111, mail a@x.com")
    assert r.summary()["pii_by_type"] == {"card": 1, "email": 1}
    assert {d.recognizer for d in r.detections} == {"CreditCardRecognizer", "EmailRecognizer"}
    assert "4111 1111" not in repr(r.detections) and "a@x.com" not in str(r.summary())


@pytest.mark.parametrize(
    "masked, kinds",
    [
        ("card [CARD_1] failed", []),
        ("my card 4111 1111 1111 1112 failed", ["long_number"]),  # mistyped card: still sensitive
        ("write to someone at x@y.com", ["email_like"]),
        ("deposit 25000 USD, leverage 1:2000, within 24 hours", []),
    ],
)
def test_residual_suspects(masked, kinds):
    from copilot.pipeline.pii import residual_suspects  # noqa: PLC0415

    assert residual_suspects(masked) == kinds

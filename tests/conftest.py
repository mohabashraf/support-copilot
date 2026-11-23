"""Fixtures shared by several test files (pytest loads this file automatically)."""

import os

import pytest

# Tests never send traces to LangSmith (a real key in .env must not make tests hit the network).
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["OTEL_ENABLED"] = "false"  # nor OpenTelemetry data to a collector

from copilot.config import Settings
from copilot.db import open_pool
from tests.pg import DB


@pytest.fixture(scope="session")
def pool():
    """A small pool on the test database, made by the same open_pool as the app."""
    p = open_pool(Settings.model_construct(database_url=DB, db_pool_size=2))
    yield p
    p.close()


# ---- A fake risk classifier for unit tests (the real one loads bge-m3, ~2 GB) -----------------

from copilot.pipeline import workflow as _wf  # noqa: E402
from copilot.pipeline.risk_classifier import (  # noqa: E402
    RISKS,
    InjectionScore,
    RiskResult,
    RiskSignal,
)
from copilot.tickets import service as _service  # noqa: E402

_FAKE_WORDS = {"buy": "trading_advice", "complaint": "complaint", "approve": "kyc_decision"}


class FakeRiskClassifier:
    """Deterministic stand-in: flags a risk when a trigger word appears. Quality is measured by
    `copilot eval risks` with the real model, not here."""

    def injection(self, text: str) -> InjectionScore:
        """ "pretend you are" = an injection no exact pattern knows (weak model signal)."""
        if "pretend you are" in text.lower():
            return InjectionScore(0.61, "weak", -1)
        return InjectionScore(0.02, None, -1)

    def predict(self, text: str) -> RiskResult:
        r = RiskResult(probabilities={k: 0.0 for k in RISKS}, category="other")
        for word, kind in _FAKE_WORDS.items():
            if word in text.lower():
                r.probabilities[kind] = 0.9
                r.signals.append(
                    RiskSignal(kind, 0.9, "strong", "classifier", -1, f"fake: {word}")
                )
        return r


class FakeInjectionGuard:
    """Stand-in for NeuralTrust: "pretend you are" is an attack (strong). Its quality is measured
    by `copilot eval injection` with the real model, not here."""

    def score(self, text: str):
        from copilot.pipeline.injection_guard import GuardScore  # noqa: PLC0415

        if "pretend you are" in text.lower():
            return GuardScore(0.97, "strong", "fake-guard")
        return GuardScore(0.01, None, "fake-guard")


class _Bound:
    """What `with_structured_output(schema)` returns: the schema travels with the call, so the
    two classifiers running in parallel threads can share one FakeChat."""

    def __init__(self, chat, schema):
        self.chat, self.schema = chat, schema

    def invoke(self, messages, config=None):
        return self.chat.invoke(messages, config, schema=self.schema)


class FakeChat:
    """Stand-in for ChatAnthropic in unit tests (no network, no cost). Answers classify_safety
    and classify_triage from keywords; the real model's quality is measured by the evals.
    `calls` / `fail_next` count and break the SAFETY call (as before); `triage_calls` /
    `triage_fail_next` do the same for triage. `triage_overrides` sets triage fields."""

    def __init__(self):
        self.model, self.calls, self.fail_next = "fake-haiku", 0, 0
        self.triage_calls, self.triage_fail_next = 0, 0
        self.triage_overrides: dict = {}

    def with_structured_output(self, schema, method=None, include_raw=False):
        return _Bound(self, schema)

    def invoke(self, messages, config=None, schema=None):
        from langchain_core.messages import AIMessage  # noqa: PLC0415

        from copilot.pipeline.schemas import TriageOut  # noqa: PLC0415

        raw = AIMessage("{}", usage_metadata={"input_tokens": 900, "output_tokens": 60,
                                              "total_tokens": 960})  # fmt: skip
        body = messages[-1].content.split("<body>")[1].split("</body>")[0]
        if schema is TriageOut:
            self.triage_calls += 1
            if self.triage_fail_next:
                self.triage_fail_next -= 1
                return {"raw": raw, "parsed": None, "parsing_error": ValueError("bad json")}
            return {"raw": raw, "parsed": _fake_triage(body, self.triage_overrides),
                    "parsing_error": None}  # fmt: skip
        self.calls += 1
        if self.fail_next:
            self.fail_next -= 1
            return {"raw": raw, "parsed": None, "parsing_error": ValueError("bad json")}
        return {"raw": raw, "parsed": _fake_safety(body), "parsing_error": None}


def _topic(low: str) -> tuple[str, str]:
    if "buy" in low:
        return "trading_advice_request", "none"
    if "complaint" in low:
        return "complaint", "complaint"
    if "approve" in low:
        return "verification", "kyc"
    if "withdraw" in low:
        return "withdrawal", "none"
    if "deposit" in low:
        return "deposit", "none"
    return "other", "none"


def _fake_safety(body: str):
    from copilot.pipeline.schemas import Intent, SafetyOut  # noqa: PLC0415

    low = body.lower()
    topic, decision = _topic(low)
    if "minimum deposit" in low:  # kept as before: tests rely on it being "other"
        topic = "other"
    quote = body.split(".")[0].split("?")[0].strip() or body.strip() or "-"
    return SafetyOut(
        intents=[Intent(topic=topic, decision_requested=decision, text=quote)],
        vulnerable_customer="lost everything" in low,
        security_sensitive="email" in low or "2fa" in low,
        injection_suspected="as the ai" in low,
    )


def _fake_triage(body: str, overrides: dict):
    from copilot.pipeline.schemas import TriageOut  # noqa: PLC0415

    low = body.lower()
    topic, _ = _topic(low)
    if "minimum deposit" in low:
        topic = "other"
    arabic = any("\u0600" <= ch <= "\u06ff" for ch in body)
    fields = {
        "category": topic,
        "urgency": "critical" if "blocked" in low else "low",
        "language": "ar" if arabic else "en",
        "reply_language": "ar" if arabic else "en",
        "sentiment": "angry" if "worst" in low else "neutral",
        "needs_account_action": "not received" in low,
        "search_query": body.strip()[:80] or "-",
        "confidence": 0.9,
    }
    return TriageOut(**{**fields, **overrides})


@pytest.fixture(autouse=True)
def _fake_risk_classifier(monkeypatch):
    """Graph runs without explicit Deps use the fakes instead of loading the real models."""
    monkeypatch.setattr(
        _wf,
        "default_deps",
        lambda: _wf.Deps(FakeRiskClassifier(), FakeInjectionGuard(), FakeChat()),
    )


@pytest.fixture(autouse=True)
def _fresh_checkpoints():
    """Ticket numbers restart in every test: never resume another test's checkpoint."""
    _service.default_app.cache_clear()
    yield
    _service.default_app.cache_clear()

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


class FakeChat:
    """Stand-in for ChatAnthropic in unit tests (no network, no cost). Answers classify_safety
    from keywords; the real model's quality is measured by the end-to-end eval, not here.
    `calls` counts invocations; `fail_next` makes the next N calls return invalid output."""

    def __init__(self):
        self.model, self.calls, self.fail_next = "fake-haiku", 0, 0

    def with_structured_output(self, schema, method=None, include_raw=False):
        return self

    def invoke(self, messages, config=None):
        from langchain_core.messages import AIMessage  # noqa: PLC0415

        from copilot.pipeline.schemas import Intent, SafetyOut  # noqa: PLC0415

        self.calls += 1
        raw = AIMessage("{}", usage_metadata={"input_tokens": 900, "output_tokens": 60,
                                              "total_tokens": 960})  # fmt: skip
        if self.fail_next:
            self.fail_next -= 1
            return {"raw": raw, "parsed": None, "parsing_error": ValueError("bad json")}
        body = messages[-1].content.split("<body>")[1].split("</body>")[0]
        low = body.lower()
        topic, decision = "other", "none"
        if "buy" in low:
            topic = "trading_advice_request"
        elif "complaint" in low:
            topic, decision = "complaint", "complaint"
        elif "approve" in low:
            topic, decision = "verification", "kyc"
        quote = body.split(".")[0].split("?")[0].strip() or body.strip() or "-"
        parsed = SafetyOut(
            intents=[Intent(topic=topic, decision_requested=decision, text=quote)],
            vulnerable_customer="lost everything" in low,
            security_sensitive="email" in low or "2fa" in low,
            injection_suspected="as the ai" in low,
        )
        return {"raw": raw, "parsed": parsed, "parsing_error": None}


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

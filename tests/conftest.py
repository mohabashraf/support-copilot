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


@pytest.fixture(autouse=True)
def _fake_risk_classifier(monkeypatch):
    """Graph runs without explicit Deps use the fake instead of loading bge-m3."""
    monkeypatch.setattr(_wf, "default_deps", lambda: _wf.Deps(FakeRiskClassifier()))

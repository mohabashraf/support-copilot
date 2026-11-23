"""Tracing is configured from .env and is off without a key."""

import os

from pydantic import SecretStr

from copilot.config import Settings
from copilot.tracing import configure_tracing


def _st(**kw) -> Settings:
    base = dict(
        langsmith_tracing=True,
        langsmith_endpoint="https://api.smith.langchain.com",
        langsmith_agent_id="support-copilot",
        langsmith_agent_environment="local",
        langsmith_api_key=None,
    )
    return Settings.model_construct(**(base | kw))


def test_off_without_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert configure_tracing(_st()) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_on_with_key_sets_agent_address_and_drops_legacy_project(monkeypatch):
    for k in (
        "LANGSMITH_TRACING",
        "LANGSMITH_AGENT_ID",
        "LANGSMITH_AGENT_ENVIRONMENT",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LANGSMITH_PROJECT", "old-project")
    assert configure_tracing(_st(langsmith_api_key=SecretStr("k"))) is True
    assert os.environ["LANGSMITH_AGENT_ID"] == "support-copilot"
    assert os.environ["LANGSMITH_AGENT_ENVIRONMENT"] == "local"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"
    assert "LANGSMITH_PROJECT" not in os.environ


def test_telemetry_off_by_setting():
    from copilot.telemetry import setup_telemetry  # noqa: PLC0415

    assert setup_telemetry(Settings.model_construct(otel_enabled=False)) is False

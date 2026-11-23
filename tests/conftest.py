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

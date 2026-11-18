"""Fixtures shared by several test files (pytest loads this file automatically)."""

import pytest

from copilot.config import Settings
from copilot.db import open_pool
from tests.pg import DB


@pytest.fixture(scope="session")
def pool():
    """A small pool on the test database, made by the same open_pool as the app."""
    p = open_pool(Settings.model_construct(database_url=DB, db_pool_size=2))
    yield p
    p.close()

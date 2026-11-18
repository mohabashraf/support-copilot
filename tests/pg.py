"""Test database helpers. Tests that need Postgres skip when it isn't running."""

import os

import psycopg

DB = os.environ.get("TEST_DATABASE_URL", "postgresql://copilot:copilot@localhost:5433/copilot")


def db_up() -> bool:
    try:
        psycopg.connect(DB, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False

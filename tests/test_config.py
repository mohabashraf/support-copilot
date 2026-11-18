"""Settings: .env is the source of truth, nothing silently falls back to a default."""

import pytest
from pydantic import ValidationError

from copilot.config import ROOT, Settings

FULL = {
    "VECTOR_STORE": "memory",
    "DATABASE_URL": "postgresql://u:p@localhost:5433/db",
    "DB_POOL_SIZE": "5",
    "EMBEDDING_MODEL": "multilingual-e5-small",
    "KB_RELOAD_SECONDS": "30",
    "LOG_LEVEL": "INFO",
    "LOG_FORMAT": "text",
    "LOG_FILE": "logs/test.log",
    "LOG_MAX_MB": "10",
    "LOG_BACKUPS": "5",
    "HOST": "127.0.0.1",
    "PORT": "8000",
}


def _env_file(tmp_path, values: dict[str, str]):
    f = tmp_path / ".env"
    f.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return f


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    for k in FULL:  # real environment variables would override the test .env
        monkeypatch.delenv(k, raising=False)


def test_reads_every_value_from_env_file(tmp_path):
    s = Settings(_env_file=_env_file(tmp_path, FULL))
    assert (s.vector_store, s.port, s.log_format) == ("memory", 8000, "text")
    assert s.log_file == ROOT / "logs/test.log"  # relative → repo root, not the current folder


def test_missing_value_fails_instead_of_defaulting(tmp_path):
    values = {k: v for k, v in FULL.items() if k != "PORT"}
    with pytest.raises(ValidationError, match="port"):
        Settings(_env_file=_env_file(tmp_path, values))


def test_invalid_value_fails(tmp_path):
    with pytest.raises(ValidationError, match="vector_store"):
        Settings(_env_file=_env_file(tmp_path, FULL | {"VECTOR_STORE": "pgvecter"}))


def test_empty_log_file_means_console_only(tmp_path):
    assert Settings(_env_file=_env_file(tmp_path, FULL | {"LOG_FILE": ""})).log_file is None


def test_env_example_has_every_setting():
    """.env.example is the template for .env: a new setting must be added there too."""
    Settings(_env_file=ROOT / ".env.example")


def test_pool_size_must_be_positive(tmp_path):
    with pytest.raises(ValidationError, match="db_pool_size"):
        Settings(_env_file=_env_file(tmp_path, FULL | {"DB_POOL_SIZE": "0"}))

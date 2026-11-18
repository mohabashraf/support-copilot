"""Settings. The .env file at the repo root is the single source of truth.

- No defaults in code: every setting must be in .env (template: .env.example).
  A missing or invalid value stops the app at startup with a clear error.
- A real environment variable with the same name overrides .env (how servers, Docker and CI
  configure apps), but it is never the only place a setting is documented.
- .env is found from the repo root, so commands work from any folder.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # src/copilot/config.py → repo root
ENV_FILE = ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    vector_store: Literal["pgvector", "memory"]
    database_url: str
    embedding_model: str

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    log_format: Literal["json", "text"]  # console format; the log file is always JSON
    log_file: Path | None  # rotated JSON lines; empty in .env = console only
    log_max_mb: int
    log_backups: int

    host: str
    port: int

    @field_validator("log_file", mode="before")
    @classmethod
    def _log_file(cls, v: str | None) -> Path | None:
        """Empty = no file. A relative path is taken from the repo root, not the current folder."""
        if not v:
            return None
        p = Path(v)
        return p if p.is_absolute() else ROOT / p


class SettingsError(RuntimeError):
    """.env is missing a setting or has an invalid value."""


@lru_cache
def get_settings() -> Settings:
    """Read .env once per process. Tests can call get_settings.cache_clear() to re-read."""
    try:
        return Settings()
    except ValidationError as err:
        problems = "\n".join(
            f"  {'.'.join(map(str, e['loc'])).upper()}: {e['msg']}" for e in err.errors()
        )
        hint = "" if ENV_FILE.exists() else f"\n{ENV_FILE} not found: cp .env.example .env"
        raise SettingsError(f"Fix these settings in .env:\n{problems}{hint}") from None

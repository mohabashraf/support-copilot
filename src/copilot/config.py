"""Settings from environment variables / .env. Code reads settings from here, never hard-coded."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    vector_store: Literal["pgvector", "memory"] = "pgvector"
    database_url: str = "postgresql://copilot:copilot@localhost:5433/copilot"
    embedding_model: str = "multilingual-e5-small"

    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    log_format: Literal["json", "text"] = (
        "json"  # console format; json for servers/log tools, text while developing
    )
    log_file: str | None = "logs/copilot.log"  # JSON lines, rotated; empty = console only
    log_max_mb: int = 10
    log_backups: int = 5
    host: str = "127.0.0.1"
    port: int = 8000


def get_settings() -> Settings:
    return Settings()

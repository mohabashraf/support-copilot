"""Settings from environment variables / .env. Code reads settings from here, never hard-coded."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    vector_store: Literal["pgvector", "memory"] = "pgvector"
    database_url: str = "postgresql://copilot:copilot@localhost:5433/copilot"
    embedding_model: str = "multilingual-e5-small"


def get_settings() -> Settings:
    return Settings()

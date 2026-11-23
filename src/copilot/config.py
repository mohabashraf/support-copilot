"""Settings. The .env file at the repo root is the single source of truth.

- No defaults in code: every setting must be in .env (template: .env.example).
  A missing or invalid value stops the app at startup with a clear error.
- A real environment variable with the same name overrides .env (how servers, Docker and CI
  configure apps), but it is never the only place a setting is documented.
- .env is found from the repo root, so commands work from any folder.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # src/copilot/config.py → repo root
ENV_FILE = ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    vector_store: Literal["pgvector", "memory"]
    database_url: str
    db_pool_size: int = Field(ge=1)
    embedding_model: str

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    log_format: Literal["json", "text"]  # console format; the log file is always JSON
    log_file: Path | None  # rotated JSON lines; empty in .env = console only
    log_max_mb: int
    log_backups: int

    host: str
    port: int
    api_docs: bool  # Swagger /docs, /redoc, /openapi.json: true locally, false in production
    api_keys: str  # name:role:sha256,... (server/auth.py); empty = everything locked

    # Ticket workflow (D2, D3, D4). Model IDs and thresholds are config, never code.
    llm_classify_model: str
    llm_draft_model: str
    llm_timeout_s: float = Field(gt=0)
    retrieval_top_k: int = Field(ge=1)
    weak_retrieval_threshold: (
        float  # best cosine below this → weak_retrieval (per embedding model)
    )
    confidence_threshold: float = Field(ge=0, le=1)  # classification confidence below → route
    low_resource_languages: Annotated[list[str], NoDecode]  # comma-separated in .env, e.g. ckb,sw
    audit_log: Path  # append-only JSONL, one record per processed ticket

    # PII masking (pipeline/pii.py): gliner = multilingual names/addresses; spacy = English only
    pii_ner: Literal["gliner", "spacy"]
    pii_gliner_model: str
    pii_gliner_threshold: float = Field(ge=0, le=1)

    # PII vault (pipeline/pii_vault.py): encrypted placeholder → value mapping with an expiry
    pii_encryption_key: SecretStr  # Fernet key; never logged
    pii_retention_days: int = Field(ge=1)

    # End-to-end monitoring (D6): OpenTelemetry → OTLP → Grafana (telemetry.py)
    otel_enabled: bool
    otel_exporter_otlp_endpoint: str  # OTLP/HTTP base URL of the Collector
    otel_service_name: str

    # LLM tracing (D6): LangSmith, for debugging only. NOT the audit log (PRD §6b).
    langsmith_tracing: bool
    langsmith_endpoint: str
    langsmith_agent_id: str  # stable agent ID in the LangSmith workspace
    langsmith_agent_environment: Literal["local", "development", "staging", "production"]
    langsmith_api_key: SecretStr | None  # empty in .env = tracing off; never logged

    @field_validator("langsmith_api_key", mode="before")
    @classmethod
    def _empty_key(cls, v: str | None) -> str | None:
        return v or None

    @field_validator("low_resource_languages", mode="before")
    @classmethod
    def _csv(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, list):
            return v
        return [x.strip() for x in v.split(",") if x.strip()]

    @field_validator("audit_log", mode="before")
    @classmethod
    def _audit_log(cls, v: str) -> Path:
        p = Path(v)
        return p if p.is_absolute() else ROOT / p

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

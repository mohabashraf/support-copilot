"""Load what the app needs from settings. Used by the server and the CLI.

Serving is READ-ONLY (D8) and keeps NO in-memory copy of the KB (D9):
- VECTOR_STORE=pgvector: every request reads Postgres. Search returns chunks and vectors from
  one JOIN; the explorer and /health read the chunks table. A `copilot kb index` run is visible
  to every server process at once, with no reload step.
- VECTOR_STORE=memory: demo/test mode, everything is built in memory from knowledge_base/raw.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from psycopg_pool import ConnectionPool

from copilot.audit import AuditLog
from copilot.config import Settings
from copilot.kb.ingest import build_kb
from copilot.kb.store import KBReader, KBStore
from copilot.log import get_logger
from copilot.pipeline.injection_guard import InjectionGuard
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import InMemoryPiiVault, PgPiiVault, PiiVault
from copilot.pipeline.risk_classifier import RiskClassifier, load_injection_rules
from copilot.retrieval.embeddings import get_embedder
from copilot.retrieval.index import InMemoryIndex, PgVectorIndex, VectorIndex
from copilot.retrieval.indexer import index_kb
from copilot.retrieval.search import Retriever
from copilot.tickets.store import InMemoryTicketStore, PgTicketStore, TicketStore


class NotIndexedError(RuntimeError):
    """Postgres has no KB, or no vectors for this embedding model."""


@dataclass(frozen=True)
class Services:
    kb: KBReader  # KBStore (Postgres, live) or KnowledgeBase (memory mode, tests)
    retriever: Retriever | None = None
    masker: Masker | None = None  # PII masking before the graph (pipeline/pii.py)
    vault: PiiVault | None = None  # encrypted placeholder → value mapping (pipeline/pii_vault.py)
    tickets: TicketStore | None = None  # the job queue (tickets/store.py)
    audit: AuditLog | None = None  # legal record, append-only (audit.py)
    risk: object | None = None  # pipeline.risk_classifier.RiskClassifier (on bge-m3)
    guard: object | None = None  # pipeline.injection_guard.InjectionGuard (NeuralTrust)


log = get_logger(__name__)

SETUP_HINT = """\
Fix: put your Claude key in .env (ANTHROPIC_API_KEY=...) and restart. API keys: the reviewer demo
keys in .env.example, or your own:
  docker compose run --rm --no-deps -v "$PWD:/work" -w /work app copilot setup --replace-demo
(writes the plain keys to .support-copilot-keys and postman/local.postman_environment.json)."""


# Where the vault key lives when PII_ENCRYPTION_KEY is empty: a file in a Docker volume
# (docker-compose.yml: app-data:/app/data), created on first start, reused after.
PII_KEY_FILE = Path(os.environ.get("PII_KEY_FILE", "data/pii.key"))


def pii_key(st: Settings) -> str:
    """The vault key: PII_ENCRYPTION_KEY if set, else the key file (created once, atomically, so
    the app and the worker starting together can never end up with two different keys)."""
    key = st.pii_encryption_key.get_secret_value()
    if key:
        return key
    if not PII_KEY_FILE.exists():
        PII_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PII_KEY_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(Fernet.generate_key().decode(), encoding="utf-8")
        tmp.chmod(0o600)
        try:
            os.link(tmp, PII_KEY_FILE)  # atomic: fails if another process created it first
            log.warning(
                "PII_ENCRYPTION_KEY empty: generated a vault key",
                extra={"file": str(PII_KEY_FILE)},
            )
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    return PII_KEY_FILE.read_text(encoding="utf-8").strip()


def require_secrets(st: Settings) -> None:
    """Fail at startup with one clear message (not a traceback) when .env is not filled in."""
    missing = []
    try:
        Fernet(pii_key(st).encode())
    except ValueError:
        missing.append("PII_ENCRYPTION_KEY is not a valid Fernet key")
    if not st.anthropic_api_key.get_secret_value():
        missing.append("ANTHROPIC_API_KEY is empty")
    if not st.api_keys.strip():
        missing.append("API_KEYS is empty (every endpoint would be locked)")
    if any(e.split(":")[0].startswith("demo-") for e in st.api_keys.split(",") if e):
        log.warning("DEMO API keys in use (from .env.example): fine for a local review only; "
                    "replace them with `copilot setup --replace-demo`")  # fmt: skip
    if missing:
        raise SystemExit(
            "Configuration incomplete (.env):\n  - " + "\n  - ".join(missing) + "\n\n" + SETUP_HINT
        )


# No-token injection detector (MIT), measured with `copilot eval injection`: 16/18 at 0.7% false
# alarms vs 18/18 for the gated default (pipeline/injection_guard.py, D12).
FALLBACK_GUARD_MODEL = "NeuralTrust/prompt-guard-oss-small"


def load_guard(st: Settings) -> InjectionGuard:
    """The configured guard; if it is gated and not accessible (no or wrong HUGGINGFACE_API_KEY,
    licence not accepted), the measured no-token fallback, with a warning: never no guard."""
    rules = load_injection_rules()
    th = {"weak": rules["guard_weak"], "strong": rules["guard_strong"]}
    token = st.huggingface_api_key.get_secret_value() if st.huggingface_api_key else None
    try:
        return InjectionGuard(st.injection_guard_model, th, token)
    except OSError as err:  # transformers: gated/private repo without access → OSError
        if st.injection_guard_model == FALLBACK_GUARD_MODEL:
            raise
        log.warning(
            "injection guard model not accessible; using the no-token fallback",
            extra={"model": st.injection_guard_model, "fallback": FALLBACK_GUARD_MODEL,
                   "has_token": bool(token), "reason": str(err).splitlines()[0][:120]},
        )  # fmt: skip
        return InjectionGuard(FALLBACK_GUARD_MODEL, th, None)


def load_services(
    st: Settings, pool: ConnectionPool | None = None, warm_masker: bool = True
) -> Services:
    """KB reader + retriever for serving. Never writes: indexing is `copilot kb index` (D8).

    `pool` is required for VECTOR_STORE=pgvector (see copilot/db.py open_pool).
    """
    require_secrets(st)
    if st.vector_store == "memory":
        kb = build_kb()
        embedder, index = get_embedder(st.embedding_model), InMemoryIndex()
        index_kb(kb, embedder, index)
        vault = InMemoryPiiVault(pii_key(st), st.pii_retention_days)
        masker = Masker.from_settings(st)
        tickets = InMemoryTicketStore()
        return Services(
            kb, Retriever(embedder, index), masker, vault, tickets, AuditLog(st.audit_log)
        )

    if pool is None:
        raise ValueError("VECTOR_STORE=pgvector needs a connection pool (copilot.db.open_pool)")
    store = KBStore(pool)
    embedder = get_embedder(st.embedding_model)
    index = PgVectorIndex(pool, st.embedding_model, embedder.dim)
    require_indexed(store, index, st.embedding_model)
    vault = PgPiiVault(pool, pii_key(st), st.pii_retention_days)
    masker = Masker.from_settings(st)
    if warm_masker:  # the API masks; the worker doesn't, so it skips the ~10 s GLiNER load
        masker.warm_up()  # load GLiNER at startup, not on the first customer's ticket
    tickets = PgTicketStore(pool)
    risk = RiskClassifier(embedder, model_name=st.embedding_model)  # trains in ~1 s (cached)
    risk.warm_up()  # first bge-m3 call at startup, not on the first customer's ticket
    guard = load_guard(st)
    guard.warm_up()
    return Services(
        store,
        Retriever(embedder, index),
        masker,
        vault,
        tickets,
        AuditLog(st.audit_log),
        risk,
        guard,
    )


def require_indexed(store: KBStore, index: VectorIndex, model: str) -> None:
    """At startup: the KB exists and every chunk version in it has a vector for this model.

    (Correctness while serving doesn't depend on this: the search JOIN only ever returns
    matching versions. This catches the setup mistake of serving a model nobody indexed.)
    """
    versions = set(store.ordered_versions())
    if not versions:
        raise NotIndexedError("no knowledge base in Postgres: run `uv run copilot kb index`")
    missing = versions - index.versions()
    if missing:
        raise NotIndexedError(
            f"{len(missing)} chunks have no {model} vectors: "
            f"run `uv run copilot kb index --model {model}`"
        )

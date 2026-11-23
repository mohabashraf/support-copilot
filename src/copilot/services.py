"""Load what the app needs from settings. Used by the server and the CLI.

Serving is READ-ONLY (D8) and keeps NO in-memory copy of the KB (D9):
- VECTOR_STORE=pgvector: every request reads Postgres. Search returns chunks and vectors from
  one JOIN; the explorer and /health read the chunks table. A `copilot kb index` run is visible
  to every server process at once, with no reload step.
- VECTOR_STORE=memory: demo/test mode, everything is built in memory from knowledge_base/raw.
"""

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from copilot.audit import AuditLog
from copilot.config import Settings
from copilot.kb.ingest import build_kb
from copilot.kb.store import KBReader, KBStore
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import InMemoryPiiVault, PgPiiVault, PiiVault
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


def load_services(
    st: Settings, pool: ConnectionPool | None = None, warm_masker: bool = True
) -> Services:
    """KB reader + retriever for serving. Never writes: indexing is `copilot kb index` (D8).

    `pool` is required for VECTOR_STORE=pgvector (see copilot/db.py open_pool).
    """
    if st.vector_store == "memory":
        kb = build_kb()
        embedder, index = get_embedder(st.embedding_model), InMemoryIndex()
        index_kb(kb, embedder, index)
        vault = InMemoryPiiVault(st.pii_encryption_key.get_secret_value(), st.pii_retention_days)
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
    vault = PgPiiVault(pool, st.pii_encryption_key.get_secret_value(), st.pii_retention_days)
    masker = Masker.from_settings(st)
    if warm_masker:  # the API masks; the worker doesn't, so it skips the ~10 s GLiNER load
        masker.warm_up()  # load GLiNER at startup, not on the first customer's ticket
    tickets = PgTicketStore(pool)
    return Services(
        store, Retriever(embedder, index), masker, vault, tickets, AuditLog(st.audit_log)
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

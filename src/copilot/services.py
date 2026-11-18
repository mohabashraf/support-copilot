"""Load what the app needs (KB + retriever) from settings. Used by the server and the CLI.

Serving is READ-ONLY (D8):
- VECTOR_STORE=pgvector: the KB and its vectors are read from Postgres, as written by
  `copilot kb index`. Missing or out-of-date → a clear error, never a silent rebuild.
- VECTOR_STORE=memory: demo/test mode, everything is built in memory from knowledge_base/raw.

Hot reload: `KBWatcher` notices when `copilot kb index` changed the stored KB and hands the
server a complete new `Services`, which it swaps in with one assignment.
"""

from collections.abc import Callable
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from copilot.config import Settings
from copilot.kb.ingest import build_kb
from copilot.kb.models import KnowledgeBase
from copilot.kb.store import KBStore
from copilot.log import get_logger
from copilot.retrieval.embeddings import get_embedder
from copilot.retrieval.index import InMemoryIndex, PgVectorIndex, VectorIndex
from copilot.retrieval.indexer import index_kb
from copilot.retrieval.search import Retriever

log = get_logger(__name__)


class NotIndexedError(RuntimeError):
    """Postgres has no KB, or no up-to-date vectors for this embedding model."""


@dataclass(frozen=True)
class Services:
    """Everything a request needs, as ONE object: swapping it is atomic, so a request never
    sees a new KB with an old retriever (or the reverse)."""

    kb: KnowledgeBase
    retriever: Retriever | None = None


def load_services(st: Settings, pool: ConnectionPool | None = None) -> Services:
    """KB + retriever for serving. Never writes: indexing is `copilot kb index` (D8).

    `pool` is required for VECTOR_STORE=pgvector (see copilot/db.py open_pool).
    """
    if st.vector_store == "memory":
        kb = build_kb()
        embedder, index = get_embedder(st.embedding_model), InMemoryIndex()
        index_kb(kb, embedder, index)
        return Services(kb, Retriever(kb, embedder, index))

    if pool is None:
        raise ValueError("VECTOR_STORE=pgvector needs a connection pool (copilot.db.open_pool)")
    kb = KBStore(pool).load()
    if kb is None:
        raise NotIndexedError("no knowledge base in Postgres: run `uv run copilot kb index`")
    embedder = get_embedder(st.embedding_model)  # cached: a reload doesn't reload the model
    index = PgVectorIndex(pool, st.embedding_model, embedder.dim)
    require_fresh(kb, index, st.embedding_model)
    return Services(kb, Retriever(kb, embedder, index))


def require_fresh(kb: KnowledgeBase, index: VectorIndex, model: str) -> None:
    """The index must hold exactly this KB's chunks, each with the same content hash."""
    if index.hashes() != kb.chunk_hashes:
        raise NotIndexedError(
            f"vectors for {model} do not match the stored KB: "
            f"run `uv run copilot kb index --model {model}`"
        )


class KBWatcher:
    """Detects a new KB written by `copilot kb index` and loads it, without a restart.

    Built from two functions so it can be tested without a database:
    - stored_hashes(): chunk_id → content_hash of the KB in the store (a cheap query)
    - load(): a complete new Services (raises NotIndexedError if its vectors aren't ready)
    """

    def __init__(
        self, stored_hashes: Callable[[], dict[str, str]], load: Callable[[], Services]
    ) -> None:
        self.stored_hashes, self.load = stored_hashes, load

    @classmethod
    def for_postgres(cls, st: Settings, pool: ConnectionPool) -> "KBWatcher":
        return cls(KBStore(pool).hashes, lambda: load_services(st, pool))

    def check(self, current: Services) -> Services | None:
        """A new Services if the stored KB changed and is ready; otherwise None (keep serving)."""
        if self.stored_hashes() == current.kb.chunk_hashes:
            return None
        try:
            new = self.load()
        except NotIndexedError as err:
            # e.g. the indexer is still running, or another model was indexed: retry next tick.
            log.warning(
                "kb changed but not ready; keeping the current one", extra={"why": str(err)}
            )
            return None
        log.info(
            "kb reloaded",
            extra={
                "old_kb_hash": current.kb.kb_hash,
                "new_kb_hash": new.kb.kb_hash,
                "chunks": len(new.kb.chunks),
            },
        )
        return new

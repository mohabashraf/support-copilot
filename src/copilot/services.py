"""Load what the app needs (KB + retriever) from settings. Used by the server and the CLI.

Serving is READ-ONLY (D8):
- VECTOR_STORE=pgvector: the KB and its vectors are read from Postgres, as written by
  `copilot kb index`. Missing or out-of-date → a clear error, never a silent rebuild.
- VECTOR_STORE=memory: demo/test mode, everything is built in memory from knowledge_base/raw.
"""

from dataclasses import dataclass

from copilot.config import Settings
from copilot.kb.models import KnowledgeBase
from copilot.retrieval.index import VectorIndex
from copilot.retrieval.search import Retriever


class NotIndexedError(RuntimeError):
    """Postgres has no KB, or no up-to-date vectors for this embedding model."""


@dataclass
class Services:
    kb: KnowledgeBase
    retriever: Retriever | None = None


def load_services(st: Settings) -> Services:
    from copilot.retrieval.embeddings import get_embedder

    if st.vector_store == "memory":
        from copilot.kb.ingest import build_kb
        from copilot.retrieval.index import InMemoryIndex
        from copilot.retrieval.indexer import index_kb

        kb = build_kb()
        embedder, index = get_embedder(st.embedding_model), InMemoryIndex()
        index_kb(kb, embedder, index)
        return Services(kb, Retriever(kb, embedder, index))

    from copilot.kb.store import KBStore
    from copilot.retrieval.index import PgVectorIndex

    kb = KBStore(st.database_url).load()
    if kb is None:
        raise NotIndexedError("no knowledge base in Postgres: run `uv run copilot kb index`")
    embedder = get_embedder(st.embedding_model)
    index = PgVectorIndex(st.database_url, st.embedding_model, embedder.dim)
    require_fresh(kb, index, st.embedding_model)
    return Services(kb, Retriever(kb, embedder, index))


def require_fresh(kb: KnowledgeBase, index: VectorIndex, model: str) -> None:
    """The index must hold exactly this KB's chunks, each with the same content hash."""
    if index.hashes() != kb.chunk_hashes:
        raise NotIndexedError(
            f"vectors for {model} do not match the stored KB: "
            f"run `uv run copilot kb index --model {model}`"
        )

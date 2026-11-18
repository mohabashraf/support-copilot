"""Retriever: question → ranked chunks with scores.

The best score is one abstain signal (`weak_retrieval`).
"""

import time
from dataclasses import dataclass

from copilot.kb.models import KnowledgeBase
from copilot.log import get_logger
from copilot.retrieval.embeddings import Embedder
from copilot.retrieval.index import VectorIndex

log = get_logger(__name__)


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    score: float  # cosine similarity, higher = closer in meaning
    rank: int  # 1 = best


class Retriever:
    def __init__(self, kb: KnowledgeBase, embedder: Embedder, index: VectorIndex):
        self.kb, self.embedder, self.index = kb, embedder, index

    def build_index(self) -> None:
        """Embed every chunk and store it. Run once per KB version / embedding model."""
        t0 = time.perf_counter()
        texts = [f"{c.section}\n{c.text}" for c in self.kb.chunks]
        vectors = self.embedder.embed_passages(texts)
        self.index.upsert(
            [c.id for c in self.kb.chunks],
            vectors,
            [list(c.flags) for c in self.kb.chunks],
            self.kb.kb_hash,
        )
        log.info(
            "index built",
            extra={
                "chunks": len(texts),
                "model": self.embedder.name,
                "index": type(self.index).__name__,
                "kb_hash": self.kb.kb_hash,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        )

    def search(self, query: str, k: int = 5, exclude_flags: tuple[str, ...] = ()) -> list[Hit]:
        t0 = time.perf_counter()
        qv = self.embedder.embed_queries([query])[0]
        hits = [
            Hit(cid, score, rank)
            for rank, (cid, score) in enumerate(self.index.search(qv, k, exclude_flags), 1)
        ]
        # No query text in logs (may contain PII): length, top chunk, score, timing only.
        log.debug(
            "search",
            extra={
                "query_chars": len(query),
                "k": k,
                "top": hits[0].chunk_id if hits else None,
                "best": round(hits[0].score, 4) if hits else None,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        )
        return hits

    def best_score(self, query: str) -> float:
        hits = self.search(query, k=1)
        return hits[0].score if hits else 0.0


def make_retriever(kb: KnowledgeBase, store: str, model: str, database_url: str) -> Retriever:
    """Build a retriever from settings. Rebuilds the pgvector table only if the KB changed."""
    from copilot.retrieval.embeddings import get_embedder
    from copilot.retrieval.index import InMemoryIndex, PgVectorIndex

    embedder = get_embedder(model)
    if store == "memory":
        r = Retriever(kb, embedder, InMemoryIndex())
        r.build_index()
        return r
    index = PgVectorIndex(database_url, model, embedder.dim)
    r = Retriever(kb, embedder, index)
    if index.kb_hash() != kb.kb_hash:
        r.build_index()
    return r

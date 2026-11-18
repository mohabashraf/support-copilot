"""Retriever: question → ranked chunks with scores. Read-only: it never writes the index.

The best score is one abstain signal (`weak_retrieval`). Writing is retrieval/indexer.py (D8).
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

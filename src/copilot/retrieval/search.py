"""Retriever: question → ranked chunks with scores. The best score is the abstain signal (weak_retrieval)."""

from dataclasses import dataclass

from copilot.kb.models import KnowledgeBase
from copilot.retrieval.embeddings import Embedder
from copilot.retrieval.index import VectorIndex


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
        texts = [f"{c.section}\n{c.text}" for c in self.kb.chunks]
        vectors = self.embedder.embed_passages(texts)
        self.index.upsert([c.id for c in self.kb.chunks], vectors, [list(c.flags) for c in self.kb.chunks], self.kb.kb_hash)

    def search(self, query: str, k: int = 5, exclude_flags: tuple[str, ...] = ()) -> list[Hit]:
        qv = self.embedder.embed_queries([query])[0]
        return [Hit(cid, score, rank) for rank, (cid, score) in enumerate(self.index.search(qv, k, exclude_flags), 1)]

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

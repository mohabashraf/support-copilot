"""Retriever + indexes with a FAKE embedder (word-count vectors).

Tests the plumbing, not model quality.
Model quality is measured by `copilot eval retrieval`, not by unit tests."""

import os

import numpy as np
import pytest

from copilot.kb.ingest import build_kb
from copilot.retrieval.index import InMemoryIndex, PgVectorIndex
from copilot.retrieval.search import Retriever

VOCAB = [
    "minimum",
    "deposit",
    "withdrawal",
    "pending",
    "documents",
    "proof",
    "address",
    "leverage",
    "islamic",
    "swap",
    "verify",
    "steps",
    "trade",
    "currencies",
    "usd",
    "hours",
]


class FakeEmbedder:
    name, dim = "fake", len(VOCAB)

    def _vec(self, text: str) -> np.ndarray:
        words = text.lower().replace("?", " ").replace(".", " ").split()
        v = np.array([sum(w.startswith(t) for w in words) for t in VOCAB], dtype=np.float32) + 1e-3
        return v / np.linalg.norm(v)

    def embed_passages(self, texts):
        return np.stack([self._vec(t) for t in texts])

    embed_queries = embed_passages


@pytest.fixture(scope="module")
def kb():
    return build_kb()


def _retriever(kb, index):
    r = Retriever(kb, FakeEmbedder(), index)
    r.build_index()
    return r


def test_memory_index_finds_obvious_chunk(kb):
    hits = _retriever(kb, InMemoryIndex()).search("minimum deposit", k=3)
    # Both min-deposit chunks are valid answers (graded labels: required + optional).
    assert {h.chunk_id for h in hits[:2]} == {"faq#min-deposit", "open#faq-min-initial-deposit"}
    assert [h.rank for h in hits] == [1, 2, 3]
    assert hits[0].score >= hits[1].score >= hits[2].score


def test_exclude_flags(kb):
    r = _retriever(kb, InMemoryIndex())
    ids = [
        h.chunk_id
        for h in r.search("withdrawal steps", k=26, exclude_flags=("defective", "promo"))
    ]
    assert "withdraw#steps" not in ids and "promo#devices" not in ids
    assert len(ids) == 24


DB = os.environ.get("TEST_DATABASE_URL", "postgresql://copilot:copilot@localhost:5433/copilot")


def _db_up() -> bool:
    try:
        import psycopg

        psycopg.connect(DB, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_up(), reason="Postgres + pgvector not running (docker compose up -d)")
def test_pgvector_matches_memory(kb):
    """Same vectors in both indexes → same ranking.

    Proves the SQL (cosine, ORDER BY, flags filter) is right.
    """
    mem = _retriever(kb, InMemoryIndex())
    pg = _retriever(kb, PgVectorIndex(DB, "fake_test", FakeEmbedder.dim))
    for q in [
        "minimum deposit",
        "documents proof of address",
        "withdrawal pending",
        "islamic swap",
    ]:
        a = [(h.chunk_id, round(h.score, 4)) for h in mem.search(q, k=5)]
        b = [(h.chunk_id, round(h.score, 4)) for h in pg.search(q, k=5)]
        assert [x[0] for x in a][:1] == [x[0] for x in b][:1], q
        assert [x[1] for x in a] == [x[1] for x in b], q
    flt = [h.chunk_id for h in pg.search("withdrawal steps", k=26, exclude_flags=("defective",))]
    assert "withdraw#steps" not in flt

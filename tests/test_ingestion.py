"""D8: ingestion is incremental and the only writer; serving is read-only.

Fake embedder that COUNTS what it embeds, so we can prove only changed chunks are re-embedded.
"""

import pytest

from copilot.kb.ingest import build_kb
from copilot.kb.models import KnowledgeBase, diff
from copilot.retrieval.index import InMemoryIndex, PgVectorIndex
from copilot.retrieval.indexer import index_kb
from copilot.services import NotIndexedError, require_fresh
from tests.test_retrieval import DB, FakeEmbedder, _db_up


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.embedded = 0

    def embed_passages(self, texts):
        self.embedded += len(texts)
        return super().embed_passages(texts)


def _edit(kb: KnowledgeBase, chunk_id: str, text: str) -> KnowledgeBase:
    """A copy of the KB with one chunk's text changed (as if the page was edited)."""
    chunks = [c.model_copy(update={"text": text}) if c.id == chunk_id else c for c in kb.chunks]
    return KnowledgeBase.from_chunks(chunks)


def _drop(kb: KnowledgeBase, chunk_id: str) -> KnowledgeBase:
    return KnowledgeBase.from_chunks([c for c in kb.chunks if c.id != chunk_id])


def test_diff():
    d = diff({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "X", "z": "9"})
    assert (d.added, d.changed, d.removed, d.unchanged) == (["c"], ["b"], ["z"], 1)


def _check_incremental(index):
    kb, emb = build_kb(), CountingEmbedder()

    d = index_kb(kb, emb, index)  # first run: everything is new
    assert len(d.added) == 26 and emb.embedded == 26

    d = index_kb(kb, emb, index)  # nothing changed: nothing embedded
    assert d.is_empty and d.unchanged == 26 and emb.embedded == 26

    kb2 = _edit(kb, "faq#min-deposit", "The minimum deposit is 10 USD.")
    d = index_kb(kb2, emb, index)  # one FAQ edited: one chunk re-embedded
    assert d.changed == ["faq#min-deposit"] and emb.embedded == 27
    assert index.hashes() == kb2.chunk_hashes

    kb3 = _drop(kb2, "promo#devices")
    d = index_kb(kb3, emb, index)  # one chunk removed: deleted, nothing embedded
    assert d.removed == ["promo#devices"] and emb.embedded == 27
    assert "promo#devices" not in index.hashes()


def test_incremental_in_memory():
    _check_incremental(InMemoryIndex())


def test_require_fresh_refuses_a_stale_index():
    kb, index = build_kb(), InMemoryIndex()
    with pytest.raises(NotIndexedError, match="copilot kb index"):
        require_fresh(kb, index, "fake")  # empty index
    index_kb(kb, FakeEmbedder(), index)
    require_fresh(kb, index, "fake")  # up to date: no error
    with pytest.raises(NotIndexedError):
        require_fresh(_edit(kb, "faq#history", "edited"), index, "fake")  # KB moved on


pg = pytest.mark.skipif(not _db_up(), reason="Postgres + pgvector not running")


@pg
def test_incremental_pgvector():
    index = PgVectorIndex(DB, "fake_incremental_test", FakeEmbedder.dim)
    index.conn.execute(f"TRUNCATE {index.table}")
    _check_incremental(index)


@pg
def test_kb_store_round_trip_and_sync():
    from copilot.kb.store import KBStore

    store = KBStore(DB, table="test_kb_chunks")
    store.conn.execute("TRUNCATE test_kb_chunks")
    assert store.load() is None  # never indexed

    kb = build_kb()
    assert len(store.sync(kb).added) == 26
    loaded = store.load()
    assert loaded == kb and loaded.kb_hash == kb.kb_hash  # same chunks, same order, same hash

    kb2 = _drop(_edit(kb, "faq#history", "edited"), "promo#devices")
    d = store.sync(kb2)
    assert d.changed == ["faq#history"] and d.removed == ["promo#devices"]
    assert store.load() == kb2

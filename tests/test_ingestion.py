"""D8 + D9: ingestion is incremental and the only writer; search is always consistent.

Fake embedder that COUNTS what it embeds, so we can prove only changed chunks are re-embedded.
"""

import pytest
from fastapi.testclient import TestClient

from copilot.kb.ingest import build_kb
from copilot.kb.models import Chunk, KnowledgeBase, Source, diff
from copilot.kb.store import KBStore
from copilot.retrieval.index import InMemoryIndex, PgVectorIndex
from copilot.retrieval.indexer import index_kb
from copilot.retrieval.search import Retriever
from copilot.server.app import create_app
from copilot.services import NotIndexedError, Services, require_indexed
from tests.pg import db_up
from tests.test_retrieval import FakeEmbedder, _truncate


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


NEW = Chunk(
    id="fees#intro",
    kind="intro",
    section="Fees",
    text="Fees. Withdrawal fees depend on the payment method.",
    sources=[Source(page_title="Fees", page_url="https://example.test/fees/")],
)


def _add(kb: KnowledgeBase, chunk: Chunk = NEW) -> KnowledgeBase:
    return KnowledgeBase.from_chunks([*kb.chunks, chunk])


def test_diff():
    d = diff({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "X", "z": "9"})
    assert (d.added, d.changed, d.removed, d.unchanged) == (["c"], ["b"], ["z"], 1)


def _check_incremental(index, store=None):
    kb, emb = build_kb(), CountingEmbedder()

    d = index_kb(kb, emb, index, store)  # first run: everything is new
    assert len(d.added) == 26 and emb.embedded == 26

    d = index_kb(kb, emb, index, store)  # nothing changed: nothing embedded
    assert d.is_empty and d.unchanged == 26 and emb.embedded == 26

    kb2 = _edit(kb, "faq#min-deposit", "The minimum deposit is 10 USD.")
    d = index_kb(kb2, emb, index, store)  # one FAQ edited: one chunk re-embedded
    assert d.changed == ["faq#min-deposit"] and emb.embedded == 27
    assert index.versions() == kb2.versions  # the old version's vector was removed

    kb3 = _drop(kb2, "promo#devices")
    d = index_kb(kb3, emb, index, store)  # one chunk removed: deleted, nothing embedded
    assert d.removed == ["promo#devices"] and emb.embedded == 27
    assert index.versions() == kb3.versions


def test_incremental_in_memory():
    _check_incremental(InMemoryIndex())


def test_server_test_app_reads_kb_through_the_reader():
    client = TestClient(create_app(Services(build_kb())))
    assert client.get("/health").json()["chunks"] == 26


pg = pytest.mark.skipif(not db_up(), reason="Postgres + pgvector not running")


@pytest.fixture
def pg_kb(pool):
    """Empty test tables: a store and an index that JOINs with it."""
    store = KBStore(pool, table="test_kb_chunks")
    index = PgVectorIndex(pool, "fake_incremental_test", FakeEmbedder.dim, "test_kb_chunks")
    _truncate(pool, store.table, index.table)
    return store, index


@pg
def test_incremental_pgvector(pg_kb):
    _check_incremental(pg_kb[1], pg_kb[0])


@pg
def test_kb_store_round_trip_and_sync(pg_kb):
    store, _ = pg_kb
    assert store.load() is None  # never indexed
    kb = build_kb()
    assert len(store.sync(kb).added) == 26
    assert store.load() == kb and store.summary() == (kb.kb_hash, 26)  # same hash, same order
    kb2 = _drop(_edit(kb, "faq#history", "edited"), "promo#devices")
    d = store.sync(kb2)
    assert d.changed == ["faq#history"] and d.removed == ["promo#devices"]
    assert store.load() == kb2


@pg
def test_search_is_consistent_while_the_indexer_runs(pg_kb):
    """The bug D9 fixes: vectors are written before chunks. A search in between must never
    return a chunk the store doesn't have, nor new text with an old vector (or the reverse)."""
    store, index = pg_kb
    kb = build_kb()
    index_kb(kb, FakeEmbedder(), index, store)
    r = Retriever(FakeEmbedder(), index)

    kb2 = _add(_edit(kb, "faq#min-deposit", "The minimum deposit is 10 USD."))
    new = [c for c in kb2.chunks if (c.id, c.content_hash) not in index.versions()]
    index.upsert(new, FakeEmbedder().embed_passages([c.embed_text for c in new]))  # step 1 only

    served = {c.id: c for c in store.all_chunks()}  # what the store holds right now (old KB)
    for q in ["minimum deposit", "withdrawal fees payment method"]:
        for h in r.search(q, k=26):
            assert h.chunk == served[h.chunk_id]  # every hit is the stored version, text included
    assert "fees#intro" not in {h.chunk_id for h in r.search("fees", k=26)}

    store.sync(kb2)  # step 2: the new versions become searchable in one transaction
    hits = {h.chunk_id: h.chunk for h in r.search("minimum deposit fees", k=27)}
    assert (
        hits["faq#min-deposit"].text == "The minimum deposit is 10 USD." and "fees#intro" in hits
    )


@pg
def test_server_sees_a_new_kb_immediately_without_reload(pg_kb):
    store, index = pg_kb
    kb = build_kb()
    index_kb(kb, FakeEmbedder(), index, store)
    retriever = Retriever(FakeEmbedder(), index)
    client = TestClient(create_app(Services(store, retriever)))
    assert client.get("/health").json() == {"status": "ok", "kb_hash": kb.kb_hash, "chunks": 26}

    kb2 = _add(kb)
    index_kb(kb2, FakeEmbedder(), index, store)  # `copilot kb index` while the server runs
    assert client.get("/health").json()["chunks"] == 27
    assert store.get_chunk("fees#intro") is not None
    hits = retriever.search("withdrawal fees payment method", k=3)  # what the drafter will use
    assert "fees#intro" in [h.chunk_id for h in hits]


@pg
def test_require_indexed(pg_kb, pool):
    store, index = pg_kb
    with pytest.raises(NotIndexedError, match="no knowledge base"):
        require_indexed(store, index, "fake")
    kb = build_kb()
    store.sync(kb)  # chunks but no vectors
    with pytest.raises(NotIndexedError, match="26 chunks have no fake vectors"):
        require_indexed(store, index, "fake")
    index_kb(kb, FakeEmbedder(), index, store)
    require_indexed(store, index, "fake")  # no error


def test_report_counts_embedding_work_for_a_new_model():
    """KB unchanged but a fresh index (e.g. a new model): the report must say 26 embedded."""
    kb, index = build_kb(), InMemoryIndex()
    index_kb(kb, FakeEmbedder(), index)
    rep = index_kb(kb, FakeEmbedder(), InMemoryIndex())  # same KB, empty index
    assert rep.embedded == 26
    assert index_kb(kb, FakeEmbedder(), index).embedded == 0

"""Ingestion pipeline (D8): the ONLY code that writes the knowledge base and its vectors.

Run by `copilot kb index`. Embeds only new or changed chunks and deletes removed ones, so
adding one page to a 1,000-chunk KB embeds that page's chunks, not all 1,000.
"""

import time

from copilot.kb.models import Diff, KnowledgeBase, diff
from copilot.kb.store import KBStore
from copilot.log import get_logger
from copilot.retrieval.embeddings import Embedder
from copilot.retrieval.index import VectorIndex

log = get_logger(__name__)


def index_kb(
    kb: KnowledgeBase, embedder: Embedder, index: VectorIndex, store: KBStore | None = None
) -> Diff:
    """Bring `index` (and `store`, if given) up to date with `kb`. Returns what changed."""
    t0 = time.perf_counter()
    d = diff(kb.chunk_hashes, index.hashes())
    todo = [kb.get(cid) for cid in d.added + d.changed]
    if todo:
        index.upsert(
            [c.id for c in todo],
            embedder.embed_passages([c.embed_text for c in todo]),
            [list(c.flags) for c in todo],
            [c.content_hash for c in todo],
        )
    if d.removed:
        index.delete(d.removed)
    # Chunks are written LAST: the server only sees a new KB once its vectors exist.
    if store is not None:
        store.sync(kb)
    log.info(
        "kb indexed",
        extra={
            "kb_hash": kb.kb_hash,
            "model": embedder.name,
            "added": len(d.added),
            "changed": len(d.changed),
            "removed": len(d.removed),
            "unchanged": d.unchanged,
            "embedded": len(todo),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    )
    return d

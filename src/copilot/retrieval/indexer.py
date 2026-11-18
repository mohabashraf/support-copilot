"""Ingestion pipeline (D8, D9): the ONLY code that writes the knowledge base and its vectors.

Run by `copilot kb index`. Embeds only chunk versions that have no vector yet, so adding one
page to a 1,000-chunk KB embeds that page's chunks, not all 1,000.

Order, so a server searching during the run always gets matching text and vectors:
1. add vectors for the new versions   (not searchable yet: no chunk row has that version)
2. write the chunks, one transaction  (the new versions become searchable at once)
3. delete vectors nobody uses now     (old versions no longer match any chunk row)
"""

import time
from dataclasses import asdict, dataclass

from copilot.kb.models import Diff, KnowledgeBase, diff
from copilot.kb.store import KBStore
from copilot.log import get_logger
from copilot.retrieval.embeddings import Embedder
from copilot.retrieval.index import VectorIndex

log = get_logger(__name__)


@dataclass(frozen=True)
class IndexReport(Diff):
    """What changed in the KB (Diff) plus the vector work actually done for this model."""

    embedded: int = 0  # can differ from added + changed, e.g. a model indexed for the first time
    vectors_deleted: int = 0


def index_kb(
    kb: KnowledgeBase, embedder: Embedder, index: VectorIndex, store: KBStore | None = None
) -> IndexReport:
    """Bring `index` (and `store`, if given) up to date with `kb`. Returns what changed."""
    t0 = time.perf_counter()
    stored = index.versions()
    before = dict(stored)  # chunk_id → hash of what was indexed (for the report)
    todo = [c for c in kb.chunks if (c.id, c.content_hash) not in stored]
    if todo:
        index.upsert(todo, embedder.embed_passages([c.embed_text for c in todo]))
    d = store.sync(kb) if store is not None else diff(kb.chunk_hashes, before)
    unused = stored - kb.versions
    if unused:
        index.delete(unused)
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
            "vectors_deleted": len(unused),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    )
    return IndexReport(**asdict(d), embedded=len(todo), vectors_deleted=len(unused))

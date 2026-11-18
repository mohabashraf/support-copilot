from fastapi import APIRouter, Depends

from copilot.kb.store import KBReader
from copilot.server.deps import get_kb

router = APIRouter(tags=["health"])


@router.get("/health")
def health(kb: KBReader = Depends(get_kb)) -> dict:
    kb_hash, chunks = kb.summary()  # live: shows a new `kb index` run immediately
    return {"status": "ok", "kb_hash": kb_hash, "chunks": chunks}

from fastapi import APIRouter, Depends

from copilot.kb.models import KnowledgeBase
from copilot.server.deps import get_kb

router = APIRouter(tags=["health"])


@router.get("/health")
def health(kb: KnowledgeBase = Depends(get_kb)) -> dict:
    return {"status": "ok", "kb_hash": kb.kb_hash, "chunks": len(kb.chunks)}

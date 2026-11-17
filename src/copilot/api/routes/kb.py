"""Knowledge-base endpoints (FR-9 explorer data)."""

from fastapi import APIRouter, Depends, HTTPException

from copilot.api.deps import get_kb
from copilot.kb.models import Chunk, KnowledgeBase

router = APIRouter(prefix="/kb", tags=["knowledge base"])


@router.get("/chunks")
def list_chunks(flag: str | None = None, kb: KnowledgeBase = Depends(get_kb)) -> list[Chunk]:
    """All chunks, optionally only those with a given flag (e.g. ?flag=defective)."""
    return [c for c in kb.chunks if flag is None or flag in c.flags]


@router.get("/chunks/{chunk_id:path}")
def get_chunk(chunk_id: str, kb: KnowledgeBase = Depends(get_kb)) -> Chunk:
    try:
        return kb.get(chunk_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"chunk not found: {chunk_id}")

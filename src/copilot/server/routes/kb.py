"""Knowledge-base endpoints (FR-9 explorer data). Read live from the KB store (D9)."""

from fastapi import APIRouter, Depends, HTTPException

from copilot.kb.models import Chunk
from copilot.kb.store import KBReader
from copilot.server.deps import get_kb

router = APIRouter(prefix="/kb", tags=["knowledge base"])


@router.get("/chunks")
def list_chunks(flag: str | None = None, kb: KBReader = Depends(get_kb)) -> list[Chunk]:
    """All chunks, optionally only those with a given flag (e.g. ?flag=defective)."""
    return [c for c in kb.all_chunks() if flag is None or flag in c.flags]


@router.get("/chunks/{chunk_id:path}")
def get_chunk(chunk_id: str, kb: KBReader = Depends(get_kb)) -> Chunk:
    chunk = kb.get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"chunk not found: {chunk_id}")
    return chunk

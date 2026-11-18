"""Search playground (FR-9.2): question → top-k chunks with scores and the abstain decision."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from copilot.retrieval.search import Retriever
from copilot.server.deps import get_retriever

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str
    k: int = 5


class SearchHit(BaseModel):
    rank: int
    chunk_id: str
    score: float
    section: str
    page_urls: list[str]
    flags: list[str]


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    best_score: float
    abstain_threshold: float | None
    would_abstain: bool | None  # None until a threshold is chosen from the eval


@router.post("/search")
def search(
    req: SearchRequest, request: Request, retriever: Retriever = Depends(get_retriever)
) -> SearchResponse:
    hits = retriever.search(req.query, k=req.k)
    best = hits[0].score if hits else 0.0
    t = getattr(request.app.state, "abstain_threshold", None)
    return SearchResponse(
        query=req.query,
        hits=[
            SearchHit(
                rank=h.rank,
                chunk_id=h.chunk_id,
                score=round(h.score, 4),
                section=retriever.kb.get(h.chunk_id).section,
                page_urls=retriever.kb.get(h.chunk_id).page_urls,
                flags=list(retriever.kb.get(h.chunk_id).flags),
            )
            for h in hits
        ],
        best_score=round(best, 4),
        abstain_threshold=t,
        would_abstain=(best < t) if t is not None else None,
    )

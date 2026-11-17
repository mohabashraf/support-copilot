"""FastAPI app. Phase 1: KB endpoints. Later: ticket view, search playground, Phase 2 webhook."""

from fastapi import FastAPI, HTTPException

from copilot.kb.ingest import build_kb
from copilot.kb.models import Chunk


def create_app() -> FastAPI:
    app = FastAPI(title="Support co-pilot", version="0.1.0")
    kb = build_kb()  # built once at startup; 26 chunks, a few KB in memory

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "kb_hash": kb.kb_hash, "chunks": len(kb.chunks)}

    @app.get("/kb/chunks")
    def list_chunks(flag: str | None = None) -> list[Chunk]:
        """All chunks, optionally only those with a given flag (e.g. ?flag=defective)."""
        return [c for c in kb.chunks if flag is None or flag in c.flags]

    @app.get("/kb/chunks/{chunk_id:path}")
    def get_chunk(chunk_id: str) -> Chunk:
        try:
            return kb.get(chunk_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"chunk not found: {chunk_id}")

    return app


app = create_app()

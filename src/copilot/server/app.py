"""Builds the FastAPI app (main.py runs it). Routes stay thin: they call kb/, retrieval/."""

from fastapi import FastAPI

from copilot.config import get_settings
from copilot.kb.ingest import build_kb
from copilot.server.middleware import log_requests
from copilot.server.routes import health, kb, search


def create_app(with_retriever: bool = True) -> FastAPI:
    app = FastAPI(title="Support co-pilot", version="0.1.0")
    app.middleware("http")(log_requests)
    app.state.kb = build_kb()  # built once at startup
    app.include_router(health.router)
    app.include_router(kb.router)
    if with_retriever:  # loads the embedding model + connects to the vector store (D7)
        from copilot.retrieval.search import make_retriever

        st = get_settings()
        app.state.retriever = make_retriever(
            app.state.kb, st.vector_store, st.embedding_model, st.database_url
        )
        app.state.abstain_threshold = None  # set from the retrieval eval once chosen
        app.include_router(search.router)
    return app

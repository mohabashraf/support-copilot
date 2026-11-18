"""Builds the FastAPI app (main.py runs it). Routes stay thin: they call kb/, retrieval/."""

from fastapi import FastAPI

from copilot.config import get_settings
from copilot.server.middleware import log_requests
from copilot.server.routes import health, kb, search
from copilot.services import Services, load_services


def create_app(services: Services | None = None) -> FastAPI:
    """uvicorn calls this with no arguments: load the KB + retriever from settings (read-only,
    D8). Tests pass their own Services, so they need no database and no model."""
    services = services or load_services(get_settings())
    app = FastAPI(title="Support co-pilot", version="0.1.0")
    app.middleware("http")(log_requests)
    app.state.kb = services.kb
    app.include_router(health.router)
    app.include_router(kb.router)
    if services.retriever is not None:
        app.state.retriever = services.retriever
        app.state.abstain_threshold = None  # set from the retrieval eval once chosen
        app.include_router(search.router)
    return app

"""FastAPI app factory. Routes are thin: they call services in kb/, retrieval/, workflow/."""

from fastapi import FastAPI

from copilot.api.routes import health, kb
from copilot.kb.ingest import build_kb


def create_app() -> FastAPI:
    app = FastAPI(title="Support co-pilot", version="0.1.0")
    app.state.kb = build_kb()  # built once at startup
    app.include_router(health.router)
    app.include_router(kb.router)
    return app


app = create_app()

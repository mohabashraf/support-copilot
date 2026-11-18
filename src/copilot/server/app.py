"""Builds the FastAPI app (main.py runs it). Routes stay thin: they call kb/, retrieval/."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from copilot.config import get_settings
from copilot.db import open_pool
from copilot.server.middleware import log_requests
from copilot.server.routes import health, kb, search
from copilot.services import Services, load_services


def create_app(services: Services | None = None) -> FastAPI:
    """uvicorn calls this with no arguments: everything comes from .env (read-only, D8).

    Tests pass their own Services, so they need no database and no model.
    """
    pool = None
    if services is None:
        st = get_settings()
        pool = open_pool(st) if st.vector_store == "pgvector" else None
        services = load_services(st, pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield  # the server runs here
        if pool is not None:
            pool.close()  # return all connections to Postgres on shutdown

    app = FastAPI(title="Support co-pilot", version="0.1.0", lifespan=lifespan)
    app.middleware("http")(log_requests)
    app.state.services = services  # set once; the data behind it is read live (D9)
    app.include_router(health.router)
    app.include_router(kb.router)
    if services.retriever is not None:
        app.state.abstain_threshold = None  # set from the retrieval eval once chosen
        app.include_router(search.router)
    return app

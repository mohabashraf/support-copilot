"""Builds the FastAPI app (main.py runs it).

Only what the business uses: the ticket API (helpdesk → co-pilot, and the agent actions:
inbox, ticket, customer details, decision) and GET /health (Docker's healthcheck).
The KB and search are inspected with the CLI (`copilot kb show`,
`copilot search`), not exposed over HTTP.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from copilot.config import get_settings
from copilot.db import open_pool
from copilot.server.auth import Principal, parse_keys
from copilot.server.middleware import log_requests
from copilot.server.routes import health, tickets, ui
from copilot.services import Services, load_services
from copilot.telemetry import instrument_app, setup_telemetry
from copilot.tracing import configure_tracing


def create_app(
    services: Services | None = None,
    api_keys: dict[str, Principal] | None = None,
    api_docs: bool = False,
) -> FastAPI:
    """uvicorn calls this with no arguments: everything comes from .env (read-only, D8).

    Tests pass their own Services and keys, so they need no database and no model.
    """
    pool = None
    if services is None:
        st = get_settings()
        api_keys = parse_keys(st.api_keys)  # malformed API_KEYS stops the server at startup
        api_docs = st.api_docs
        configure_tracing(st)
        setup_telemetry(st)  # before the pool opens, so Postgres queries are traced
        pool = open_pool(st) if st.vector_store == "pgvector" else None
        services = load_services(st, pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield  # the server runs here
        if pool is not None:
            pool.close()  # return all connections to Postgres on shutdown

    # API docs (Swagger /docs, ReDoc /redoc, /openapi.json): on for development, off in
    # production (API_DOCS=false) so the API's shape isn't published to anyone who can reach it.
    docs = {} if api_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Support co-pilot", version="0.1.0", lifespan=lifespan, **docs)
    app.state.api_keys = api_keys or {}  # empty → every protected endpoint answers 401
    app.middleware("http")(log_requests)
    instrument_app(app)
    app.state.services = services  # set once; the data behind it is read live (D9)
    app.include_router(health.router)
    if services.masker is not None:
        app.include_router(tickets.router)
        app.include_router(ui.router)  # GET /ui: the agent page (same API, caller's own keys)
    return app

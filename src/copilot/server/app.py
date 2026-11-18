"""Builds the FastAPI app (main.py runs it). Routes stay thin: they call kb/, retrieval/."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from copilot.config import get_settings
from copilot.db import open_pool
from copilot.log import get_logger
from copilot.server.middleware import log_requests
from copilot.server.routes import health, kb, search
from copilot.services import KBWatcher, Services, load_services

log = get_logger(__name__)


def create_app(
    services: Services | None = None,
    watcher: KBWatcher | None = None,
    reload_seconds: float = 0,
) -> FastAPI:
    """uvicorn calls this with no arguments: everything comes from .env (read-only, D8).

    Tests pass their own `services` (and optionally a `watcher`), so they need no database
    and no model.
    """
    pool = None
    if services is None:
        st = get_settings()
        pool = open_pool(st) if st.vector_store == "pgvector" else None
        services = load_services(st, pool)
        if pool is not None and st.kb_reload_seconds > 0:
            watcher, reload_seconds = KBWatcher.for_postgres(st, pool), st.kb_reload_seconds

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_watch(app, watcher, reload_seconds)) if watcher else None
        yield  # the server runs here
        if task:
            task.cancel()
        if pool is not None:
            pool.close()

    app = FastAPI(title="Support co-pilot", version="0.1.0", lifespan=lifespan)
    app.middleware("http")(log_requests)
    app.state.services = services  # ONE object: a reload replaces it in one assignment
    app.include_router(health.router)
    app.include_router(kb.router)
    if services.retriever is not None:
        app.state.abstain_threshold = None  # set from the retrieval eval once chosen
        app.include_router(search.router)
    return app


async def _watch(app: FastAPI, watcher: KBWatcher, every: float) -> None:
    """Every `every` seconds: has `copilot kb index` written a new KB? If so, swap it in."""
    while True:
        await asyncio.sleep(every)
        try:
            # The check queries Postgres (blocking), so it runs in a worker thread.
            new = await run_in_threadpool(watcher.check, app.state.services)
        except Exception:  # never let the watcher die: log and try again next tick
            log.exception("kb reload check failed")
            continue
        if new is not None:
            app.state.services = new

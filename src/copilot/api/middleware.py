"""Request logging: one line per API request with a request ID (returned as X-Request-ID)."""

import time
import uuid

from fastapi import Request

from copilot.log import get_logger

log = get_logger("copilot.api")


async def log_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("request failed", extra={"request_id": request_id, "method": request.method,
                                               "path": request.url.path})
        raise
    response.headers["X-Request-ID"] = request_id
    log.info("request", extra={"request_id": request_id, "method": request.method, "path": request.url.path,
                               "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1)})
    return response

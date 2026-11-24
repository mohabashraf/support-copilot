"""The agent page (GET /ui): one static HTML file, no build step, no extra dependencies.

It calls the same JSON API as Postman with the caller's own keys (kept in the browser), so it
adds no new permissions: creating a ticket needs a helpdesk key, reading and deciding an agent key.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["ui"])
PAGE = Path(__file__).resolve().parent.parent / "static" / "agent.html"


@router.get("/ui", include_in_schema=False)
def agent_page() -> FileResponse:
    return FileResponse(PAGE, media_type="text/html")

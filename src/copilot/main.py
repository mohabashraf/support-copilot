"""Main entry point.

uv run python -m copilot.main     → start the FastAPI server (the ONLY way to start it)
uv run copilot <command>          → CLI for one-off jobs (kb, search, eval)
"""

import uvicorn

from copilot.config import get_settings
from copilot.log import setup_logging


def serve(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    """Start the FastAPI server with uvicorn."""
    st = get_settings()
    setup_logging(st.log_level, st.log_format, st.log_file, st.log_max_mb, st.log_backups)
    uvicorn.run(
        # Import string + factory=True: the app is created when the server starts, not at import.
        "copilot.api.app:create_app",
        factory=True,
        host=host or st.host,
        port=port or st.port,
        reload=reload,
        log_level=st.log_level.lower(),
        log_config=None,  # don't let uvicorn replace our logging: its logs become JSON lines too
        access_log=False,  # our middleware logs each request (request_id + ms): no duplicates
    )


def main() -> None:
    """The `copilot` command (see pyproject.toml [project.scripts])."""
    from copilot.cli import app

    app()


if __name__ == "__main__":
    serve()

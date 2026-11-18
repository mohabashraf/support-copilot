"""Start the FastAPI server: `uv run python -m copilot.main`.

The CLI for one-off jobs (kb, search, eval) is separate: `uv run copilot <command>` (cli.py).
"""

import uvicorn

from copilot.config import get_settings
from copilot.log import setup_logging


def serve() -> None:
    """Start the FastAPI server with uvicorn. Host, port and log level come from settings."""
    st = get_settings()
    setup_logging(st.log_level, st.log_format, st.log_file, st.log_max_mb, st.log_backups)
    uvicorn.run(
        # Import string + factory=True: uvicorn calls create_app() when the server starts.
        # A module-level `app = create_app()` would load the embedding model on every import.
        "copilot.api.app:create_app",
        factory=True,
        host=st.host,
        port=st.port,
        log_level=st.log_level.lower(),
        log_config=None,  # don't let uvicorn replace our logging: its logs become JSON lines too
        access_log=False,  # our middleware logs each request (request_id + ms): no duplicates
    )


if __name__ == "__main__":
    serve()

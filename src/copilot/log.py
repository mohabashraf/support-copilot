"""Application logging: structured JSON lines to stdout (PRD §6b, layer 3 "app logs").

- One JSON object per line → easy to stream (`| jq`), ship to Datadog/Grafana Loki, or grep.
- Level from settings (LOG_LEVEL); format json | text (text is nicer while developing).
- Noisy libraries (torch, transformers, Hugging Face) are quieted to WARNING.
- Never log ticket or query TEXT (it can contain PII): log lengths, IDs, scores, timings instead.
  Ticket content belongs in the audit log, masked, not in app logs.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from copilot.config import Settings

NOISY = [
    "sentence_transformers",
    "transformers",
    "torch",
    "huggingface_hub",
    "urllib3",
    "httpx",
    "filelock",
    "presidio-analyzer",  # logs every recognizer it loads
    "gliner",
    "httpx2",
]
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "color_message",  # uvicorn adds a copy of msg with terminal colour codes: noise in a file
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed with extra={...} becomes a field: log.info("searched", extra={"ms": 12})
        out.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS}
        tail = " ".join(f"{k}={v}" for k, v in extras.items())
        time = self.formatTime(record, "%H:%M:%S")
        return f"{time} {record.levelname:7} {record.name}: {record.getMessage()} {tail}".rstrip()


def setup_logging(st: Settings) -> None:
    """Console in LOG_FORMAT; file (if LOG_FILE is set) always JSON. All values come from .env.

    The file rotates at LOG_MAX_MB and keeps LOG_BACKUPS old files.
    """
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter() if st.log_format == "json" else TextFormatter())
    handlers: list[logging.Handler] = [console]
    if st.log_file:
        st.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            st.log_file,
            maxBytes=st.log_max_mb * 1024 * 1024,
            backupCount=st.log_backups,
            encoding="utf-8",
        )
        fh.setFormatter(JsonFormatter())  # files are for tools: always machine-readable
        handlers.append(fh)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(st.log_level)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Presidio warns at load time about every non-English recognizer it skips: noise.
    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

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
from datetime import datetime, timezone

NOISY = ["sentence_transformers", "transformers", "torch", "huggingface_hub", "urllib3", "httpx", "filelock"]
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
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
        return f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:7} {record.name}: {record.getMessage()} {tail}".rstrip()


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

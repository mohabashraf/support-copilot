"""Logs are valid JSON lines and never contain query text."""

import json
import logging

from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.config import Settings
from copilot.log import JsonFormatter, setup_logging


def _st(level="INFO", fmt="json", log_file=None) -> Settings:
    """Logging settings for a test, without reading .env (model_construct skips loading)."""
    return Settings.model_construct(
        log_level=level, log_format=fmt, log_file=log_file, log_max_mb=10, log_backups=5
    )


def test_json_formatter_includes_extra_fields():
    rec = logging.LogRecord("copilot.x", logging.INFO, __file__, 1, "searched", (), None)
    rec.ms = 12.5
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "searched" and out["level"] == "INFO" and out["ms"] == 12.5


def test_request_log_has_request_id(capsys):
    setup_logging(_st())
    r = TestClient(create_app(with_retriever=False)).get("/health")
    assert r.headers["X-Request-ID"]
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    req = [x for x in lines if x["msg"] == "request"]
    assert req and req[-1]["path"] == "/health" and req[-1]["status"] == 200


def test_search_log_has_no_query_text(capsys):
    from copilot.kb.ingest import build_kb
    from copilot.retrieval.index import InMemoryIndex
    from copilot.retrieval.search import Retriever
    from tests.test_retrieval import FakeEmbedder

    setup_logging(_st("DEBUG"))
    r = Retriever(build_kb(), FakeEmbedder(), InMemoryIndex())
    r.build_index()
    secret = "my card 4111 1111 1111 1111 minimum deposit"
    r.search(secret)
    out = capsys.readouterr().out
    assert '"msg": "search"' in out
    assert "4111" not in out and "minimum deposit" not in out


def test_file_log_is_json_even_when_console_is_text(tmp_path):
    import logging as std_logging

    f = tmp_path / "logs" / "app.log"
    setup_logging(_st(fmt="text", log_file=f))
    std_logging.getLogger("copilot.test").info("hello file", extra={"n": 1})
    for h in std_logging.getLogger().handlers:
        h.flush()
    line = json.loads(f.read_text().splitlines()[-1])
    assert line["msg"] == "hello file" and line["n"] == 1


def test_uvicorn_color_message_is_dropped():
    rec = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, "started", (), None)
    rec.color_message = "\x1b[36mstarted\x1b[0m"
    rec.ms = 3
    out = json.loads(JsonFormatter().format(rec))
    assert "color_message" not in out and out["ms"] == 3

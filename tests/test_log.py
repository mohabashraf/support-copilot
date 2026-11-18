"""Logs are valid JSON lines and never contain query text."""

import json
import logging

from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.log import JsonFormatter, setup_logging


def test_json_formatter_includes_extra_fields():
    rec = logging.LogRecord("copilot.x", logging.INFO, __file__, 1, "searched", (), None)
    rec.ms = 12.5
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "searched" and out["level"] == "INFO" and out["ms"] == 12.5


def test_request_log_has_request_id(capsys):
    setup_logging("INFO", "json")
    r = TestClient(create_app(with_retriever=False)).get("/health")
    assert r.headers["X-Request-ID"]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    req = [x for x in lines if x["msg"] == "request"]
    assert req and req[-1]["path"] == "/health" and req[-1]["status"] == 200


def test_search_log_has_no_query_text(capsys):
    from tests.test_retrieval import FakeEmbedder
    from copilot.kb.ingest import build_kb
    from copilot.retrieval.index import InMemoryIndex
    from copilot.retrieval.search import Retriever

    setup_logging("DEBUG", "json")
    r = Retriever(build_kb(), FakeEmbedder(), InMemoryIndex())
    r.build_index()
    secret = "my card 4111 1111 1111 1111 minimum deposit"
    r.search(secret)
    out = capsys.readouterr().out
    assert '"msg": "search"' in out
    assert "4111" not in out and "minimum deposit" not in out


def test_file_log_is_json_even_when_console_is_text(tmp_path):
    import logging as L

    f = tmp_path / "logs" / "app.log"
    setup_logging("INFO", "text", str(f))
    L.getLogger("copilot.test").info("hello file", extra={"n": 1})
    for h in L.getLogger().handlers:
        h.flush()
    line = json.loads(f.read_text().splitlines()[-1])
    assert line["msg"] == "hello file" and line["n"] == 1

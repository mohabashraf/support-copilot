"""CLI commands that people rely on: API key creation and the masking report."""

import json

from typer.testing import CliRunner

from copilot import cli
from copilot.config import Settings
from copilot.server.auth import hash_key, parse_keys

runner = CliRunner()


def _settings(tmp_path):
    return Settings.model_construct(
        log_level="WARNING", log_format="text", log_file=tmp_path / "copilot.log",
        log_max_mb=1, log_backups=1, langsmith_tracing=False, langsmith_api_key=None,
        otel_enabled=False,
    )  # fmt: skip


def test_keys_new_prints_the_key_once_and_a_valid_hash_line(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    out = runner.invoke(cli.app, ["keys", "new", "sara", "agent"])
    assert out.exit_code == 0
    key = next(w for w in out.stdout.split() if w.startswith("sk_"))
    line = next(w for w in out.stdout.split() if w.startswith("sara:agent:"))
    assert line == f"sara:agent:{hash_key(key)}" and parse_keys(line)[hash_key(key)].name == "sara"
    bad = runner.invoke(cli.app, ["keys", "new", "sara", "admin"])
    assert bad.exit_code == 1


def test_pii_report_counts_types_and_flags_misses(tmp_path, monkeypatch):
    st = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: st)
    events = [
        {"msg": "ticket queued", "ticket_id": "A", "pii_ms": 100, "pii_by_type": {"card": 1},
         "pii_by_recognizer": {"CreditCardRecognizer": 1}, "pii_residual_suspect": []},
        {"msg": "pii masked", "ticket_id": "B", "pii_ms": 200, "pii_by_type": {"name": 2},
         "pii_by_recognizer": {"MultilingualNameRecognizer": 2},
         "pii_residual_suspect": ["long_number"], "trace_id": "t-1"},
    ]  # fmt: skip
    st.log_file.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    out = runner.invoke(cli.app, ["pii-report"])
    assert out.exit_code == 0
    assert "tickets masked: 2" in out.stdout  # both event names are read
    assert "card=1" in out.stdout and "name=2" in out.stdout
    assert "possible misses (residual suspects): 1" in out.stdout and "ticket B" in out.stdout

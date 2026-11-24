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


def test_setup_fills_empty_secrets_once_and_the_keys_work(tmp_path):
    from cryptography.fernet import Fernet  # noqa: PLC0415

    from copilot.config import ROOT  # noqa: PLC0415

    (tmp_path / ".env.example").write_text(
        (ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    kept = runner.invoke(cli.app, ["setup", "--dir", str(tmp_path)])
    assert kept.exit_code == 0 and "demo keys from .env.example are kept" in kept.output
    assert not (tmp_path / ".support-copilot-keys").exists()  # demo keys: nothing new to hand out
    res = runner.invoke(cli.app, ["setup", "--dir", str(tmp_path), "--replace-demo"])
    assert res.exit_code == 0, res.output
    assert "demo-" not in (tmp_path / ".env").read_text()  # the demo keys are gone
    env = dict(
        ln.split("=", 1)
        for ln in (tmp_path / ".env").read_text().splitlines()
        if "=" in ln and not ln.startswith("#")
    )
    Fernet(env["PII_ENCRYPTION_KEY"].encode())  # a valid key
    keys = dict(
        ln.split("=", 1)
        for ln in (tmp_path / ".support-copilot-keys").read_text().splitlines()
        if "=" in ln and not ln.startswith("#")
    )
    table = parse_keys(env["API_KEYS"])
    assert table[hash_key(keys["HELPDESK_KEY"])].role == "helpdesk"
    assert table[hash_key(keys["AGENT_KEY"])].role == "agent"
    pm = json.loads((tmp_path / "postman" / "local.postman_environment.json").read_text())
    assert {v["key"]: v["value"] for v in pm["values"]}["agentKey"] == keys["AGENT_KEY"]
    assert f"agent={keys['AGENT_KEY']}" in keys["UI_SIGN_IN_LINK"]  # UI link signs in
    assert "ANTHROPIC_API_KEY" in res.output  # tells the user what is still missing

    before = (tmp_path / ".env").read_text()
    again = runner.invoke(cli.app, ["setup", "--dir", str(tmp_path)])
    assert again.exit_code == 0 and "nothing to do" in again.output
    assert (tmp_path / ".env").read_text() == before  # never overwrites


def test_vault_key_is_generated_once_and_reused(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet  # noqa: PLC0415
    from pydantic import SecretStr  # noqa: PLC0415

    from copilot import services  # noqa: PLC0415

    monkeypatch.setattr(services, "PII_KEY_FILE", tmp_path / "data" / "pii.key")
    st = _settings(tmp_path).model_copy(update={"pii_encryption_key": SecretStr("")})
    first = services.pii_key(st)
    Fernet(first.encode())  # valid
    assert services.pii_key(st) == first  # the second process reads the same key
    assert oct((tmp_path / "data" / "pii.key").stat().st_mode)[-3:] == "600"
    set_key = Fernet.generate_key().decode()
    assert (
        services.pii_key(st.model_copy(update={"pii_encryption_key": SecretStr(set_key)}))
        == set_key
    )

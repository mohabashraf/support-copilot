"""API keys: who may call what; hashes only in config; fail closed."""

import pytest
from fastapi.testclient import TestClient

from copilot.kb.ingest import build_kb
from copilot.pipeline.pii import Masker
from copilot.server.app import create_app
from copilot.server.auth import hash_key, new_key, parse_keys
from copilot.services import Services
from copilot.tickets.store import InMemoryTicketStore
from tests.keys import HELPDESK, KEYS, SARA


def _client(keys=KEYS, docs=False):
    svc = Services(build_kb(), masker=Masker(ner="spacy"), tickets=InMemoryTicketStore())
    return TestClient(create_app(svc, api_keys=keys, api_docs=docs))


@pytest.mark.parametrize(
    "method, path, helpdesk, agent",
    [
        ("post", "/tickets", 202, 403),
        ("get", "/tickets", 403, 200),
        ("get", "/tickets/1", 200, 200),
        ("get", "/tickets/1/customer-details", 403, 200),
    ],
)
def test_role_matrix(method, path, helpdesk, agent):
    client = _client()
    client.post("/tickets", json={"id": "A1", "body": "hi"}, headers=HELPDESK)  # ticket 1
    body = {"json": {"id": "A2", "body": "hi"}} if method == "post" else {}
    assert getattr(client, method)(path, headers=HELPDESK, **body).status_code == helpdesk
    body = {"json": {"id": "A3", "body": "hi"}} if method == "post" else {}
    assert getattr(client, method)(path, headers=SARA, **body).status_code == agent


def test_missing_or_wrong_key_is_401_and_health_stays_open():
    client = _client()
    assert client.get("/tickets").status_code == 401
    assert client.get("/tickets", headers={"Authorization": "Bearer sk_wrong"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_no_keys_configured_locks_everything():
    client = _client(keys={})
    assert client.get("/tickets", headers=SARA).status_code == 401


def test_parse_keys_takes_hashes_not_raw_keys():
    key = new_key()
    keys = parse_keys(f"sara:agent:{hash_key(key)}, helpdesk:helpdesk:{hash_key('x')}")
    assert keys[hash_key(key)].name == "sara"
    with pytest.raises(ValueError, match="64-char"):
        parse_keys(f"sara:agent:{key}")  # someone pasted the raw key
    with pytest.raises(ValueError, match="unknown role"):
        parse_keys(f"sara:admin:{hash_key(key)}")


def test_api_docs_only_when_enabled():
    assert _client(docs=False).get("/docs").status_code == 404
    assert _client(docs=True).get("/openapi.json").status_code == 200

"""The API exposes only what the business uses: POST /tickets and GET /health."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from copilot.kb.ingest import build_kb
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import InMemoryPiiVault
from copilot.server.app import create_app
from copilot.services import Services
from copilot.tickets.store import InMemoryTicketStore
from copilot.tickets.worker import run_once
from tests.keys import HELPDESK, KEYS, SARA

vault = InMemoryPiiVault(Fernet.generate_key().decode(), retention_days=1)
tickets = InMemoryTicketStore()
# No model download in unit tests: spaCy masker, memory vault + queue, no retriever.
client = TestClient(
    create_app(
        Services(build_kb(), masker=Masker(ner="spacy"), vault=vault, tickets=tickets),
        api_keys=KEYS,
    )
)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["chunks"] == 26


def test_post_returns_a_ticket_number_and_get_shows_progress_then_result():
    r = client.post(
        "/tickets",
        headers=HELPDESK,
        json={"id": "S1", "subject": "Deposit", "body": "card 4111 1111 1111 1111 failed"},
    )
    assert r.status_code == 202
    b = r.json()
    n = b["ticket_number"]
    assert b["status"] == "queued" and r.headers["location"] == f"/tickets/{n}"
    assert "4111" not in r.text  # the response never carries a real PII value
    assert vault.reveal("S1", "[CARD_1]") == "4111 1111 1111 1111"

    queued = client.get(f"/tickets/{n}", headers=HELPDESK).json()
    assert queued["status"] == "queued" and queued["result"] is None

    assert run_once(tickets)  # the background worker
    done = client.get(f"/tickets/{n}", headers=HELPDESK).json()
    assert done["status"] == "done"
    assert [st["node"] for st in done["steps"]][0] == "pattern_check"
    assert [st["node"] for st in done["steps"]][-1] == "audit"
    assert done["seconds_processing"] is not None
    # No AI draft exists yet, so routing (real) sends it to a person; the ticket is not complete.
    routing = done["result"]["routing"]
    assert routing["decision"] == "human_required" and routing["reasons"] == ["no_draft"]
    assert done["complete"] is False and done["result"]["reply"] is None
    # 8 of the 14 steps on this path are implemented; the rest are reported as placeholders.
    assert done["steps_completed"] == "8/14"
    completed = {st["node"] for st in done["steps"] if st["status"] == "completed"}
    assert completed == {
        "pattern_check",
        "injection_check",
        "risk_classifier",
        "classify_safety",
        "classify_triage",
        "validate_classification",
        "confidence_gate",
        "route",
    }
    assert "4111" not in str(done)


def test_same_ticket_again_returns_the_same_number():
    first = client.post("/tickets", headers=HELPDESK, json={"id": "S9", "body": "hi"})
    again = client.post("/tickets", headers=HELPDESK, json={"id": "S9", "body": "hi"})
    assert (
        again.status_code == 200 and again.json()["ticket_number"] == first.json()["ticket_number"]
    )


def test_unknown_ticket_is_404():
    assert client.get("/tickets/999999", headers=SARA).status_code == 404


def test_ticket_without_body_is_rejected():
    assert client.post("/tickets", headers=HELPDESK, json={"id": "S2"}).status_code == 422


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/kb/chunks"),
        ("post", "/search"),
        ("post", "/pii/mask"),
        ("get", "/agent"),
        ("get", "/docs"),
        ("get", "/openapi.json"),
    ],
)
def test_no_endpoints_the_business_does_not_use(method, path):
    assert getattr(client, method)(path).status_code == 404


def test_possible_unmasked_pii_is_logged_as_a_warning(caplog):
    # Fails the Luhn check, so it is not masked as a card; but it is still a 16-digit number.
    client.post(
        "/tickets",
        headers=HELPDESK,
        json={"id": "S3", "body": "my card 4111 1111 1111 1112 failed"},
    )
    warnings = [r for r in caplog.records if r.getMessage() == "possible unmasked pii"]
    assert warnings and warnings[0].kinds == ["long_number"]

"""Agent endpoints (JSON): inbox, one ticket, audited customer details, decisions."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from copilot.audit import AuditLog
from copilot.kb.ingest import build_kb
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import InMemoryPiiVault
from copilot.server.app import create_app
from copilot.services import Services
from copilot.tickets.store import InMemoryTicketStore
from copilot.tickets.worker import run_once
from tests.keys import HELPDESK, KEYS, OMAR, SARA

DRAFT = "Dear [NAME_1], the payment with card [CARD_1] failed. The minimum deposit is 8 USD."


@pytest.fixture
def env(tmp_path):
    vault = InMemoryPiiVault(Fernet.generate_key().decode(), retention_days=1)
    tickets, audit = InMemoryTicketStore(), AuditLog(tmp_path / "audit.jsonl")
    svc = Services(
        build_kb(), masker=Masker(ner="spacy"), vault=vault, tickets=tickets, audit=audit
    )
    client = TestClient(create_app(svc, api_keys=KEYS))
    body = "Hi, this is Ahmed Hassan and my card 4111 1111 1111 1111 failed"
    n = client.post(
        "/tickets", json={"id": "C1", "subject": "Deposit", "body": body}, headers=HELPDESK
    ).json()["ticket_number"]
    run_once(tickets, audit)
    rec = tickets.get(n)  # the LLM nodes are placeholders today: give it a realistic draft
    tickets.finish(n, {**rec.result, "reply": DRAFT})
    return client, tickets, audit, n


def test_inbox_lists_tickets_with_counts_and_filters(env):
    client, _, _, n = env
    inbox = client.get("/tickets", headers=SARA).json()
    assert inbox["counts"] == {"done": 1}
    row = inbox["items"][0]
    assert row["ticket_number"] == n and row["progress"] == "8/8" and not row["handled_by_agent"]
    assert row["queue"] == "support_agent"
    assert client.get("/tickets", params={"status": "queued"}, headers=SARA).json()["items"] == []
    assert (
        client.get("/tickets", params={"queue": "compliance"}, headers=SARA).json()["items"] == []
    )
    assert client.get("/tickets", params={"queue": "nope"}, headers=SARA).status_code == 422
    assert "4111" not in client.get("/tickets", headers=SARA).text


def test_ticket_shows_masked_message_and_draft(env):
    client, _, _, n = env
    t = client.get(f"/tickets/{n}", headers=SARA).json()
    assert t["message"]["body"] == "Hi, this is [NAME_1] and my card [CARD_1] failed"
    assert t["result"]["reply"] == DRAFT and t["agent_action"] is None
    assert "4111" not in str(t) and "Ahmed" not in str(t)


def test_customer_details_need_an_agent_and_are_audited(env):
    client, _, audit, n = env
    assert client.get(f"/tickets/{n}/customer-details").status_code == 401  # no key
    assert client.get(f"/tickets/{n}/customer-details", headers=HELPDESK).status_code == 403
    values = client.get(f"/tickets/{n}/customer-details", headers=SARA).json()
    assert values == {"[CARD_1]": "**** 1111", "[NAME_1]": "Ahmed Hassan"}  # card never in full
    event = [e for e in audit.read(n) if e["event"] == "pii_revealed"][0]
    assert event["agent"] == "sara" and event["types"] == ["CARD", "NAME"]  # from the key


def test_send_unchanged_is_sent_filled_then_stored_masked(env):
    client, tickets, audit, n = env
    r = client.post(
        f"/tickets/{n}/decision", json={"action": "send", "final_text": DRAFT}, headers=SARA
    )  # noqa: E501
    assert r.status_code == 200
    action = r.json()["agent_action"]
    assert action["decision"] == "sent" and action["agent"] == "sara"
    assert "Ahmed" not in action["final_text_masked"] and "4111" not in action["final_text_masked"]
    event = [e for e in audit.read(n) if e["event"] == "agent_decision"][0]
    assert event["ai_draft"] == DRAFT and event["decision"] == "sent"


def test_edited_and_second_decision_and_manual(env):
    client, _, _, n = env
    r = client.post(
        f"/tickets/{n}/decision",
        json={"action": "send", "final_text": DRAFT + " Sorry!"},
        headers=SARA,
    )  # noqa: E501
    assert r.json()["agent_action"]["decision"] == "edited"
    again = client.post(f"/tickets/{n}/decision", json={"action": "manual"}, headers=OMAR)
    assert again.status_code == 409  # already handled (by sara)


def test_cannot_decide_before_the_copilot_is_done(env):
    client, _, _, _ = env
    n2 = client.post("/tickets", json={"id": "C2", "body": "hi"}, headers=HELPDESK).json()[
        "ticket_number"
    ]
    r = client.post(f"/tickets/{n2}/decision", json={"action": "manual"}, headers=SARA)
    assert r.status_code == 409


def test_send_needs_text(env):
    client, _, _, n = env
    r = client.post(
        f"/tickets/{n}/decision", json={"action": "send", "final_text": " "}, headers=SARA
    )  # noqa: E501
    assert r.status_code == 422


def test_worker_writes_the_processing_audit_record(env):
    _, _, audit, n = env
    event = [e for e in audit.read(n) if e["event"] == "ticket_processed"][0]
    assert event["steps"][-1] == "audit" and "masked_text_hash" in event

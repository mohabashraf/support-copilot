"""Tickets as background jobs: intake masks + enqueues, the worker processes, status is visible.
Store tests run on memory and on Postgres (same behaviour)."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import InMemoryPiiVault
from copilot.pipeline.schemas import Ticket
from copilot.tickets import service
from copilot.tickets.store import MAX_ATTEMPTS, InMemoryTicketStore, PgTicketStore
from copilot.tickets.worker import run_once
from tests.pg import db_up

MASKER = Masker(ner="spacy")


@pytest.fixture(params=["memory", "pg"])
def store(request):
    if request.param == "memory":
        return InMemoryTicketStore()
    if not db_up():
        pytest.skip("Postgres not running (docker compose up -d db)")
    pool = request.getfixturevalue("pool")
    with pool.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS tickets_test CASCADE")
    return PgTicketStore(pool, table="tickets_test")


def _vault():
    return InMemoryPiiVault(Fernet.generate_key().decode(), retention_days=1)


def test_intake_masks_stores_mapping_and_queues(store):
    vault = _vault()
    rec, created = service.intake(
        Ticket(id="T1", subject="card 4111 1111 1111 1111", body="mail a@x.com"),
        MASKER,
        vault,
        store,
    )
    assert created and rec.status == "queued" and rec.number >= 1
    assert rec.masked == {
        "subject": "card [CARD_1]",
        "body": "mail [EMAIL_1]",
    }  # no raw PII stored
    assert rec.pii_types == ["card", "email"]
    assert vault.reveal("T1", "[CARD_1]") == "4111 1111 1111 1111"


def test_same_ticket_id_twice_is_one_ticket(store):
    a, created_a = service.intake(Ticket(id="T1", body="hi"), MASKER, None, store)
    b, created_b = service.intake(Ticket(id="T1", body="hi again"), MASKER, None, store)
    assert created_a and not created_b and a.number == b.number


def test_worker_runs_the_graph_and_records_every_step(store):
    rec, _ = service.intake(
        Ticket(id="T1", body="What is the minimum deposit?"), MASKER, None, store
    )
    assert run_once(store) is True
    done = store.get(rec.number)
    assert done.status == "done" and done.attempts == 1
    assert done.steps == [
        "classify",
        "policy_gate",
        "retrieve",
        "draft",
        "post_check",
        "compose",
        "route",
        "audit",
    ]
    # Today every node is a placeholder: nothing is decided, and the result says so.
    assert done.result["routing"] is None and done.result["complete"] is False
    assert done.trace_id and done.finished_at
    assert run_once(store) is False  # queue empty


def test_errors_are_retried_then_marked_failed(store, monkeypatch):
    def boom(record):
        raise TimeoutError("LLM timed out")
        yield  # makes it a generator like the real _stream

    monkeypatch.setattr(service, "_stream", boom)
    rec, _ = service.intake(Ticket(id="T1", body="hi"), MASKER, None, store)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert run_once(store)
        r = store.get(rec.number)
        assert r.attempts == attempt and "LLM timed out" in r.error
        assert r.status == ("failed" if attempt == MAX_ATTEMPTS else "queued")
    assert run_once(store) is False  # failed tickets are not picked up again


def test_two_claims_never_get_the_same_ticket(store):
    for i in range(2):
        service.intake(Ticket(id=f"T{i}", body="hi"), MASKER, None, store)
    a, b = store.claim(), store.claim()
    assert a.number != b.number
    assert store.claim() is None


def test_a_crashed_workers_ticket_is_claimed_again_after_its_lease(store):
    rec, _ = service.intake(Ticket(id="T1", body="hi"), MASKER, None, store)
    assert store.claim().number == rec.number  # worker 1 takes it ... and dies
    assert store.claim() is None  # lease still valid: nobody else takes it
    if isinstance(store, InMemoryTicketStore):
        store.rows[rec.number].lease_until = datetime.now(UTC) - timedelta(seconds=1)
    else:
        with store.pool.connection() as conn:
            conn.execute("UPDATE tickets_test SET lease_until = now() - interval '1 second'")
    again = store.claim()
    assert again.number == rec.number and again.attempts == 2


def test_placeholder_nodes_are_reported_as_placeholders_not_as_work_done(store):
    """Monitoring must never report work that didn't happen."""
    from copilot.pipeline.workflow import NODE_IMPLEMENTED  # noqa: PLC0415

    rec, _ = service.intake(Ticket(id="T1", body="hi"), MASKER, None, store)
    run_once(store)
    done = store.get(rec.number)
    for step in done.step_times:
        assert step["implemented"] == NODE_IMPLEMENTED[step["node"]]
    expected = [n for n in done.steps if not NODE_IMPLEMENTED[n]]
    assert done.result["placeholder_nodes"] == expected


# ---- Inbox query and agent decisions (memory AND Postgres) ----------------------------------


def _done(store, ticket_id: str, destination: str | None):
    rec, _ = service.intake(Ticket(id=ticket_id, body="hi"), MASKER, None, store)
    claimed = store.claim()
    routing = {"decision": "human_required", "destination": destination, "reasons": []}
    store.finish(claimed.number, {"routing": routing if destination else None})
    return rec.number


def test_inbox_filters_by_status_and_queue_newest_first(store):
    a = _done(store, "A", "compliance")
    b = _done(store, "B", "senior_support")
    c = _done(store, "C", None)  # not decided
    service.intake(Ticket(id="D", body="hi"), MASKER, None, store)  # still queued
    assert [r.number for r in store.list(status="done")] == [c, b, a]
    assert [r.number for r in store.list(destination="compliance")] == [a]
    assert [r.external_id for r in store.list(status="queued")] == ["D"]
    assert len(store.list(limit=2)) == 2
    assert store.counts() == {"done": 3, "queued": 1}


def test_agent_decision_is_stored(store):
    n = _done(store, "A", "support_agent")
    store.decide(n, "edited", "sara", "Dear [NAME_1], sorry.")
    r = store.get(n)
    assert (r.agent_decision, r.agent_name, r.final_text_masked) == (
        "edited",
        "sara",
        "Dear [NAME_1], sorry.",
    )
    assert r.decided_at is not None


# ---- Concurrency: two workers at the same time never take the same ticket -------------------


def test_parallel_claims_never_take_the_same_ticket(store):
    import threading  # noqa: PLC0415

    for i in range(30):
        service.intake(Ticket(id=f"P{i}", body="hi"), MASKER, None, store)
    taken: list[int] = []
    lock = threading.Lock()

    def worker():
        while (rec := store.claim()) is not None:
            with lock:
                taken.append(rec.number)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(taken) == 30 and len(set(taken)) == 30  # every ticket exactly once

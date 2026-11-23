"""One trace per ticket, end to end: the worker continues the API's intake trace, with the time
spent in the queue as its own span (so Tempo shows POST → masking → queue wait → every step)."""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from copilot.pipeline.pii import Masker
from copilot.pipeline.schemas import Ticket
from copilot.tickets import service
from copilot.tickets.store import InMemoryTicketStore
from copilot.tickets.worker import run_once

_exporter = InMemorySpanExporter()
# The app's tracer is a proxy on the global provider; give the process a real one (no export).
trace.set_tracer_provider(tp := TracerProvider())
tp.add_span_processor(SimpleSpanProcessor(_exporter))


def test_intake_queue_wait_processing_and_every_step_share_one_trace():
    _exporter.clear()
    store = InMemoryTicketStore()
    rec, _ = service.intake(
        Ticket(id="TR1", body="What is the minimum deposit?"), Masker(ner="spacy"), None, store
    )
    assert store.get(rec.number).trace_context.startswith("00-")  # W3C traceparent stored
    run_once(store)

    spans = {s.name: s for s in _exporter.get_finished_spans()}
    for name in (
        "ticket.intake",
        "pii_mask",
        "queue.wait",
        "ticket.process",
        "node.pattern_check",
        "node.route",
    ):
        assert name in spans, name
    trace_ids = {s.context.trace_id for s in spans.values()}
    assert len(trace_ids) == 1  # ONE trace from POST to done

    intake, wait, process = spans["ticket.intake"], spans["queue.wait"], spans["ticket.process"]
    assert wait.parent.span_id == intake.context.span_id
    assert process.parent.span_id == intake.context.span_id
    assert wait.end_time <= process.start_time  # the wait ends when the worker starts


def test_end_to_end_starts_when_the_request_arrives_not_when_it_is_queued():
    store = InMemoryTicketStore()
    rec, _ = service.intake(Ticket(id="TR2", body="hi"), Masker(ner="spacy"), None, store)
    r = store.get(rec.number)
    assert r.received_at is not None and r.received_at <= r.created_at  # masking happens between

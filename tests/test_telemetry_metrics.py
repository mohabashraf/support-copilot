"""Metrics carry the honest labels the dashboard relies on (complete, decision, PII types)."""

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from copilot.pipeline.pii import Masker
from copilot.pipeline.schemas import Ticket
from copilot.tickets import service
from copilot.tickets.store import InMemoryTicketStore
from copilot.tickets.worker import run_once

_reader = InMemoryMetricReader()


@pytest.fixture(scope="module")
def reader():
    # The app's instruments are created on the global proxy meter; giving the process a real
    # provider (tests never export) binds them, so we can read what they recorded.
    metrics.set_meter_provider(MeterProvider(metric_readers=[_reader]))
    return _reader


def _points(reader, name: str) -> list[tuple[dict, float]]:
    out = []
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    for p in m.data.data_points:
                        out.append(
                            (dict(p.attributes), getattr(p, "value", getattr(p, "count", 0)))
                        )
    return out


def test_ticket_metrics_say_incomplete_and_not_decided_while_nodes_are_placeholders(reader):
    store = InMemoryTicketStore()
    service.intake(
        Ticket(id="T1", body="Hi, this is Ahmed Hassan, card 4111 1111 1111 1111"),
        Masker(ner="spacy"),
        None,
        store,
    )
    run_once(store)
    tickets = _points(reader, "copilot.tickets")
    assert ({"decision": "not_decided", "destination": "not_decided", "complete": "false"}, 1) in [
        (a, v) for a, v in tickets
    ]
    assert not any(a.get("decision") == "human_required" for a, _ in tickets)  # nothing invented
    pii = {(a["type"], a["recognizer"]) for a, v in _points(reader, "copilot.pii.detections") if v}
    assert ("card", "CreditCardRecognizer") in pii and ("name", "SpacyRecognizer") in pii
    by_node = {
        (a["node"], a["implemented"]) for a, _ in _points(reader, "copilot.graph.node.duration")
    }
    assert ("pattern_check", "true") in by_node and ("risk_classifier", "true") in by_node
    assert ("route", "false") in by_node  # placeholders are labelled as such

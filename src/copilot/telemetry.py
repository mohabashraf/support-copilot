"""End-to-end monitoring with OpenTelemetry (D6): traces, metrics and logs → OTLP → Grafana.

    app ──OTLP/HTTP──► OTel Collector ──► Tempo (traces) · Prometheus (metrics) · Loki (logs)
                                                    └────────────► Grafana dashboards + alerts

- Vendor-neutral: the same code sends to the local `grafana/otel-lgtm` container (docker compose),
  Grafana Cloud, a self-hosted LGTM cluster or Datadog: only OTEL_EXPORTER_OTLP_ENDPOINT changes.
- PUSH model: the app sends data to the Collector, so the API exposes no extra endpoint
  (it stays POST /tickets + GET /health, D5).
- What is instrumented:
  - traces: FastAPI requests, Postgres queries (psycopg), and our spans: ticket → pii_mask →
    graph nodes. Each ticket span carries `langsmith.trace_id`, so Grafana links to LangSmith.
  - metrics: the instruments below (tickets, routing, PII, latency, LLM tokens/cost).
  - logs: every log record (with its trace ID, so Grafana jumps trace ↔ logs).
- Never ticket text or PII values: only IDs, types, counts and timings (same rule as the logs).
- LangSmith stays for LLM-level debugging (prompts, tokens per call); this is the system view.
"""

import copy
import json
import logging

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from copilot.config import Settings

tracer = trace.get_tracer("copilot")
_meter = metrics.get_meter("copilot")

# ---- Metrics (Prometheus names get a unit/_total suffix, e.g. copilot_tickets_total) ---------
# Labels are low-cardinality on purpose (no ticket IDs): IDs belong in traces and logs.
TICKETS = _meter.create_counter(
    "copilot.tickets", description="Tickets processed, by routing decision and destination"
)
TICKET_MS = _meter.create_histogram(
    "copilot.ticket.duration", unit="ms", description="End-to-end processing time per ticket"
)
ROUTING_REASONS = _meter.create_counter(
    "copilot.routing.reasons", description="Routing reason codes that fired"
)
PII_DETECTIONS = _meter.create_counter(
    "copilot.pii.detections", description="PII spans masked, by type and recognizer"
)
PII_RESIDUAL = _meter.create_counter(
    "copilot.pii.residual", description="Possible unmasked PII left after masking, by kind"
)
PII_TICKETS = _meter.create_counter(
    "copilot.pii.tickets", description="Tickets masked, by whether a residual suspect was found"
)
PII_MS = _meter.create_histogram(
    "copilot.pii.duration", unit="ms", description="PII masking time per ticket"
)
NODE_MS = _meter.create_histogram(
    "copilot.graph.node.duration", unit="ms", description="Time per workflow node"
)
RISK_SIGNALS = _meter.create_counter(
    "copilot.risk.signals",
    description="Risk signals by kind, strength (strong/weak) and method (pattern/classifier)",
)
SPAM = _meter.create_counter(
    "copilot.spam", description="Tickets detected as spam/empty, by reason"
)
QUEUE_WAIT_MS = _meter.create_histogram(
    "copilot.queue.wait",
    unit="ms",
    description="Time a ticket waited in the queue before a worker took it",
)
TICKET_FAILURES = _meter.create_counter(
    "copilot.ticket.failures",
    description="Processing errors; final=true when retries are exhausted",
)
LLM_TOKENS = _meter.create_counter(
    "copilot.llm.tokens", description="LLM tokens, by model and kind (input/output/cache_read)"
)
LLM_COST = _meter.create_counter("copilot.llm.cost_usd", description="LLM cost in USD, by model")


class _SafeLoggingHandler(LoggingHandler):
    """OTel log attributes must be scalars or lists of scalars: dict extras (e.g. pii_by_type)
    are sent as JSON strings. Works on a copy so the file/console handlers are unaffected."""

    def emit(self, record: logging.LogRecord) -> None:
        record = copy.copy(record)
        for k, v in list(record.__dict__.items()):
            if isinstance(v, dict) or (
                isinstance(v, list) and any(isinstance(x, dict) for x in v)
            ):
                setattr(record, k, json.dumps(v, ensure_ascii=False, default=str))
        super().emit(record)


_configured = False


def setup_telemetry(st: Settings) -> bool:
    """Call once per process, before the DB pool opens (so psycopg is instrumented)."""
    global _configured
    if _configured or not st.otel_enabled:
        return _configured
    base = st.otel_exporter_otlp_endpoint.rstrip("/")
    resource = Resource.create(
        {
            "service.name": st.otel_service_name,
            "deployment.environment": st.langsmith_agent_environment,
        }
    )

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
    trace.set_tracer_provider(tp)

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics"), export_interval_millis=5000
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs"))
    )
    set_logger_provider(lp)
    logging.getLogger().addHandler(_SafeLoggingHandler(level=logging.INFO, logger_provider=lp))

    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor  # noqa: PLC0415

    PsycopgInstrumentor().instrument()
    _init_counters()
    _configured = True
    logging.getLogger(__name__).info("telemetry on", extra={"otlp": base})
    return True


def _init_counters() -> None:
    """Start known series at 0. Prometheus' increase() ignores the FIRST value of a new series,
    so without this the first ticket of each kind would be missing from the dashboard."""
    for decision in ("auto_draft_ok", "human_required"):
        for dest in ("none", "support_agent", "senior_support", "compliance"):
            TICKETS.add(0, {"decision": decision, "destination": dest})
    for final in ("true", "false"):
        TICKET_FAILURES.add(0, {"final": final})
    for residual in ("yes", "no"):
        PII_TICKETS.add(0, {"residual": residual})
    for kind in ("long_number", "email_like", "iban_like"):
        PII_RESIDUAL.add(0, {"kind": kind})
    for typ, rec in (
        ("card", "CreditCardRecognizer"),
        ("account", "IbanRecognizer"),
        ("account", "AccountNumberRecognizer"),
        ("email", "EmailRecognizer"),
        ("phone", "PhoneRecognizer"),
        ("doc", "DocumentNumberRecognizer"),
        ("name", "MultilingualNameRecognizer"),
        ("address", "MultilingualNameRecognizer"),
    ):
        PII_DETECTIONS.add(0, {"type": typ, "recognizer": rec})


def instrument_app(app) -> None:
    """HTTP spans + request metrics for FastAPI (health checks excluded: they are noise)."""
    if not _configured:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health")


def register_queue_gauge(store) -> None:
    """Queue depth by status (queued / processing / done / failed), read at each export (5 s).
    Registered by the worker, which owns the queue."""
    from opentelemetry.metrics import Observation  # noqa: PLC0415

    def observe(_options):
        try:
            counts = store.counts()
        except Exception:  # monitoring must never break the worker
            return []
        return [
            Observation(counts.get(s, 0), {"status": s})
            for s in ("queued", "processing", "done", "failed")
        ]

    _meter.create_observable_gauge(
        "copilot.queue.tickets", callbacks=[observe], description="Tickets in the queue, by status"
    )


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def shutdown() -> None:
    """Flush before a CLI command exits (data is exported in the background)."""
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        if hasattr(provider, "shutdown"):
            provider.shutdown()

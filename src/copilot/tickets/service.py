"""The two halves of a ticket's life, split between the API and the worker.

    API     intake(ticket)  : mask PII → vault → INSERT queued        (≈200 ms, returns a number)
    worker  process(record) : run the LangGraph workflow node by node, saving each step → done

Why split: the API answers in milliseconds whatever the LLM does; a crash or LLM timeout never
loses a ticket (it is in Postgres and is retried); workers scale by starting more of them.

Each half has its own LangSmith trace and OpenTelemetry span; both carry the ticket number.
Never ticket text or PII values in traces, metrics or logs: IDs, types, counts, timings only.
"""

import time
import traceback
import uuid

from langsmith import trace

from copilot import telemetry as tel
from copilot.audit import AuditLog, text_hash
from copilot.log import get_logger
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import PiiVault
from copilot.pipeline.schemas import Ticket
from copilot.pipeline.workflow import NODE_IMPLEMENTED, Deps, graph
from copilot.tickets.store import TicketRecord, TicketStore

log = get_logger(__name__)


def intake(
    ticket: Ticket, masker: Masker, vault: PiiVault | None, store: TicketStore
) -> tuple[TicketRecord, bool]:  # noqa: E501
    """Mask, store the mapping encrypted, enqueue. Returns (record, created). A ticket ID seen
    before returns the existing record (idempotent: webhooks are delivered more than once)."""
    run_id = uuid.uuid4()
    safe = {
        "ticket_id": ticket.id,
        "chars": {"subject": len(ticket.subject), "body": len(ticket.body)},
    }  # noqa: E501
    with (
        tel.tracer.start_as_current_span(
            "ticket.intake", attributes={"ticket.id": ticket.id}
        ) as span,  # noqa: E501
        trace(
            "ticket-intake", inputs=safe, run_id=run_id, metadata={"ticket_id": ticket.id}
        ) as root,  # noqa: E501
    ):
        with (
            tel.tracer.start_as_current_span("pii_mask") as mask_span,
            trace("pii_mask", run_type="tool", inputs=safe) as ls_mask,
        ):
            report = masker.mask_ticket(subject=ticket.subject, body=ticket.body)
            # Trace (Tempo): how masking was done. Types, recognizers, scores; never values.
            mask_span.set_attribute("pii.count", len(report.detections))
            mask_span.set_attribute("pii.types", report.types)
            mask_span.set_attribute(
                "pii.recognizers", sorted({d.recognizer for d in report.detections})
            )  # noqa: E501
            mask_span.set_attribute("pii.residual", report.residual)
            mask_span.set_attribute("pii.ms", round(report.ms, 1))
            for d in report.detections:
                mask_span.add_event(
                    "pii.detected",
                    {
                        "field": d.field,
                        "placeholder": d.placeholder,
                        "type": d.type,
                        "recognizer": d.recognizer,
                        "score": d.score,
                        "chars": d.length,
                    },
                )
            ls_mask.end(
                outputs={
                    "masked": report.masked,
                    "detections": [d.__dict__ for d in report.detections],
                    **report.summary(),
                }
            )
        pii = report.summary()
        record, created = store.create(
            ticket.id,
            report.masked,
            pii["pii_types"],
            str(run_id),
            pii_detections=[d.__dict__ for d in report.detections],
            pii_residual=report.residual,
            pii_ms=round(report.ms, 1),
        )
        if created and vault is not None:
            vault.store(ticket.id, report.mapping)
        span.set_attribute("ticket.number", record.number)
        span.set_attribute("ticket.created", created)
        root.add_metadata(
            {
                "ticket_number": record.number,
                **{k: pii[k] for k in ("pii_types", "pii_count", "pii_residual_suspect")},
            }
        )  # noqa: E501
        root.end(
            outputs={"ticket_number": record.number, "status": record.status, "created": created}
        )  # noqa: E501

    ids = {"ticket_id": ticket.id, "ticket_number": record.number}
    if created:
        _record_pii_metrics(report)
        log.info("ticket queued", extra={**ids, "intake_trace_id": str(run_id), **pii})
        if report.residual:
            log.warning("possible unmasked pii", extra={**ids, "kinds": report.residual})
    else:
        log.info("ticket already known", extra={**ids, "status": record.status})
    return record, created


def process(
    record: TicketRecord,
    store: TicketStore,
    audit: AuditLog | None = None,
    deps: Deps | None = None,
) -> None:
    """Run the workflow on one claimed ticket, saving each node as it finishes."""
    t0 = time.perf_counter()
    run_id = uuid.uuid4()
    wait_ms = (
        (record.started_at - record.created_at).total_seconds() * 1000 if record.started_at else 0
    )  # noqa: E501
    tel.QUEUE_WAIT_MS.record(max(wait_ms, 0))
    ids = {"ticket_id": record.external_id, "ticket_number": record.number}
    attrs = {
        "ticket.id": record.external_id,
        "ticket.number": record.number,
        "ticket.attempt": record.attempts,
        "langsmith.trace_id": str(run_id),
    }  # noqa: E501
    with (
        tel.tracer.start_as_current_span("ticket.process", attributes=attrs),
        trace(
            "support-copilot",
            inputs={"ticket_number": record.number, "attempt": record.attempts},
            run_id=run_id,
            metadata={
                "ticket_id": record.external_id,
                "ticket_number": record.number,
                "otel_trace_id": tel.current_trace_id(),
            },  # noqa: E501
        ) as root,
    ):
        state: dict = {}
        _steps_seen: list[str] = []
        try:
            t_prev = time.perf_counter()
            for node, update in _stream(record, deps):
                now = time.perf_counter()
                state.update({k: v for k, v in (update or {}).items() if k != "steps"})
                store.set_step(
                    record.number,
                    node,
                    trace_id=str(run_id),
                    ms=round((now - t_prev) * 1000, 1),
                    implemented=NODE_IMPLEMENTED.get(node, True),
                )
                t_prev = now
                _steps_seen.append(node)
            routing = state.get("routing")
            classification = state.get("classification")
            placeholders = [n for n in _steps_seen if not NODE_IMPLEMENTED.get(n, True)]
            result = {
                # null = not produced (by a placeholder node or not reached), never a fake value
                "classification": classification.model_dump() if classification else None,
                "reply": state.get("reply"),
                "citations": state.get("citations"),
                "routing": routing.model_dump() if routing else None,
                # Signals from the implemented checks (no customer text: names, probabilities,
                # and the nearest TRAINING example as evidence).
                "patterns": state.get("patterns"),
                "risk": state.get("risk"),
                # Honest completeness: did every node the ticket passed through do real work?
                "complete": not placeholders,
                "steps_completed": len(_steps_seen) - len(placeholders),  # real work only
                "placeholder_nodes": placeholders,
            }
            store.finish(record.number, result)
            root.end(outputs={"routing": result["routing"]})
            if audit is not None:
                audit.write(
                    "ticket_processed",
                    ticket_number=record.number,
                    ticket_id=record.external_id,
                    attempt=record.attempts,
                    masked_text_hash=text_hash(
                        record.masked.get("subject", "") + record.masked.get("body", "")
                    ),
                    pii_types=record.pii_types,
                    steps=list(_steps_seen),
                    classification=result["classification"],
                    ai_draft=result["reply"],  # produced from masked text: placeholders only
                    cited_chunks=[c.get("chunk_id") for c in result["citations"] or []],
                    patterns=result["patterns"],
                    risk_signals=(result["risk"] or {}).get("signals"),
                    routing=result["routing"],  # null when no real routing was made
                    complete=result["complete"],
                    placeholder_nodes=result["placeholder_nodes"],
                    trace_id=str(run_id),
                    intake_trace_id=record.intake_trace_id,
                )
        except Exception as err:  # every failure is recorded and retried, never dropped
            status = store.fail(record.number, f"{type(err).__name__}: {err}")
            tel.TICKET_FAILURES.add(1, {"final": str(status == "failed").lower()})
            log.error(
                "ticket failed",
                extra={
                    **ids,
                    "attempt": record.attempts,
                    "now": status,
                    "error": type(err).__name__,
                    "trace": traceback.format_exc(limit=3),
                },
            )
            root.end(error=str(err))
            return

    ms = (time.perf_counter() - t0) * 1000
    decision = routing.decision if routing else "not_decided"
    complete = not result["placeholder_nodes"]
    tel.TICKETS.add(
        1,
        {
            "decision": decision,
            "destination": (routing.destination if routing else None) or "not_decided",
            "complete": str(complete).lower(),
        },
    )
    tel.TICKET_MS.record(ms)
    for reason in routing.reasons if routing else []:
        tel.ROUTING_REASONS.add(1, {"reason": reason})
    pats = result.get("patterns") or {}
    for hit in pats.get("hits", []):
        tel.RISK_SIGNALS.add(1, {"kind": hit["kind"], "strength": "strong", "method": "pattern"})
    if pats.get("spam_reason"):
        tel.SPAM.add(1, {"reason": pats["spam_reason"]})
    for sig in (result.get("risk") or {}).get("signals", []):
        tel.RISK_SIGNALS.add(
            1, {"kind": sig["kind"], "strength": sig["strength"], "method": sig["method"]}
        )
    log.info(
        "ticket processed",
        extra={
            **ids,
            "trace_id": str(run_id),
            "decision": decision,
            "reasons": routing.reasons if routing else [],
            "complete": complete,
            "placeholder_nodes": result["placeholder_nodes"],
            "ms": round(ms, 1),
            "queue_wait_ms": round(wait_ms),
        },  # noqa: E501
    )


def _stream(record: TicketRecord, deps: Deps | None = None):
    """Yield (node, update) as each node finishes, so GET /tickets/{n} shows live progress."""
    inp = {"ticket_id": record.external_id, **record.masked}
    for chunk in graph.stream(inp, stream_mode="updates", context=deps):
        yield from chunk.items()


def _record_pii_metrics(report) -> None:
    for d in report.detections:
        tel.PII_DETECTIONS.add(1, {"type": d.type, "recognizer": d.recognizer})
    for kind in report.residual:
        tel.PII_RESIDUAL.add(1, {"kind": kind})
    tel.PII_TICKETS.add(1, {"residual": "yes" if report.residual else "no"})
    tel.PII_MS.record(report.ms)

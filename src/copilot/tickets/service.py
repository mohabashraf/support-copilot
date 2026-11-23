"""The two halves of a ticket's life, split between the API and the worker.

    API     intake(ticket)  : mask PII → vault → INSERT queued        (≈200 ms, returns a number)
    worker  process(record) : run the LangGraph workflow node by node, saving each step → done

Why split: the API answers in milliseconds whatever the LLM does; a crash or LLM timeout never
loses a ticket (it is in Postgres and is retried); workers scale by starting more of them.

Resume after a failure: the graph runs with a checkpointer (thread = the ticket number). LangGraph
saves the state after every step, so a retry continues from the step that failed instead of
starting over: finished steps (and LLM calls already paid for) are not run again. The worker uses
Postgres (PostgresSaver); tests and scripts use memory. The checkpoint is deleted once the ticket
is done; a failed ticket keeps it for debugging.

Each half has its own LangSmith trace and OpenTelemetry span; both carry the ticket number.
Never ticket text or PII values in traces, metrics or logs: IDs, types, counts, timings only.
"""

import time
import traceback
import uuid
from datetime import UTC, datetime
from functools import lru_cache

from langgraph.checkpoint.memory import InMemorySaver
from langsmith import trace

from copilot import telemetry as tel
from copilot.audit import AuditLog, text_hash
from copilot.log import get_logger
from copilot.pipeline import routing as routing_rules
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import PiiVault
from copilot.pipeline.schemas import Ticket
from copilot.pipeline.workflow import NODE_IMPLEMENTED, Deps, build_graph
from copilot.tickets.store import TicketRecord, TicketStore

log = get_logger(__name__)


def intake(
    ticket: Ticket, masker: Masker, vault: PiiVault | None, store: TicketStore
) -> tuple[TicketRecord, bool]:  # noqa: E501
    """Mask, store the mapping encrypted, enqueue. Returns (record, created). A ticket ID seen
    before returns the existing record (idempotent: webhooks are delivered more than once)."""
    received_at = datetime.now(UTC)  # end-to-end time starts here, before masking
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
            trace_context=tel.current_context_header(),  # the worker continues this trace
            received_at=received_at,
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


@lru_cache
def default_app():
    """The workflow with an in-memory checkpointer (tests, scripts). The worker passes its own
    app with PostgresSaver so resume survives a crash."""
    return build_graph(checkpointer=InMemorySaver())


def thread_id(record: TicketRecord) -> str:
    """One LangGraph thread per ticket. The helpdesk ID is included so a thread can never be
    picked up by another ticket that happens to get the same number (e.g. a recreated table)."""
    return f"ticket-{record.number}-{record.external_id}"


def process(
    record: TicketRecord,
    store: TicketStore,
    audit: AuditLog | None = None,
    deps: Deps | None = None,
    app=None,
) -> None:
    """Run the workflow on one claimed ticket, saving each node as it finishes. If an earlier
    attempt failed part-way, continue from its checkpoint."""
    app = app or default_app()
    config = {"configurable": {"thread_id": thread_id(record)}}
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
    # One trace per ticket: continue the intake's trace, with the time spent in the queue as
    # its own span, so Tempo shows POST → masking → queue wait → every graph step.
    parent = tel.context_from(record.trace_context)
    if record.attempts == 1 and record.started_at:
        tel.tracer.start_span(
            "queue.wait", context=parent, start_time=tel.ns(record.created_at), attributes=attrs
        ).end(end_time=tel.ns(record.started_at))
    with (
        tel.tracer.start_as_current_span("ticket.process", context=parent, attributes=attrs),
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
        try:
            snap = app.get_state(config)
            # resume_from = the steps still to run after a failed attempt (None = fresh run)
            resume_from = sorted(snap.next) if snap.values and snap.next else None
            finished_before = bool(snap.values) and not snap.next  # crashed before saving
            if resume_from:
                log.info(
                    "ticket resumed",
                    extra={**ids, "attempt": record.attempts, "from": resume_from},
                )
            elif not finished_before:
                store.reset_steps(record.number)
            root.add_metadata({"resumed_from": resume_from})
            t_prev = time.perf_counter()
            if not finished_before:
                for node, _update in _stream(record, deps, app, config, resume=bool(resume_from)):
                    now = time.perf_counter()
                    store.set_step(
                        record.number,
                        node,
                        trace_id=str(run_id),
                        ms=round((now - t_prev) * 1000, 1),
                        implemented=NODE_IMPLEMENTED.get(node, True),
                        attempt=record.attempts,
                    )
                    t_prev = now
            # The full state from the checkpoint: includes steps finished by earlier attempts.
            state = app.get_state(config).values
            _steps_seen = list(state.get("steps", []))
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
                "injection": state.get("injection"),  # the injection gate: detected + evidence
                "safety": state.get("safety"),  # classify_safety (LLM): intents + flags
                "llm_usage": state.get("llm_usage", []),  # tokens + cost per LLM call
                "risk": state.get("risk"),
                # Honest completeness: did every node the ticket passed through do real work?
                "complete": not placeholders,
                "steps_completed": len(_steps_seen) - len(placeholders),  # real work only
                "placeholder_nodes": placeholders,
                "resumed_from": resume_from,  # null = ran from the start
                "attempt": record.attempts,
            }
            store.finish(record.number, result)
            _drop_checkpoint(app, config, ids)
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
                    injection=result["injection"],
                    safety=result["safety"],
                    llm_cost_usd=round(sum(u["cost_usd"] for u in result["llm_usage"]), 6),
                    risk_signals=(result["risk"] or {}).get("signals"),
                    routing=result["routing"],  # null when no real routing was made
                    complete=result["complete"],
                    placeholder_nodes=result["placeholder_nodes"],
                    resumed_from=resume_from,
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
            if status == "failed":  # no more retries: a person takes it over
                failed = _failed_result(app, config, err)
                store.set_result(record.number, failed)
                _hand_over_failed(record, failed, err, audit)
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
    received = record.received_at or record.created_at
    tel.TICKET_E2E_MS.record((datetime.now(UTC) - received).total_seconds() * 1000)
    for reason in routing.reasons if routing else []:
        tel.ROUTING_REASONS.add(1, {"reason": reason})
    pats = result.get("patterns") or {}
    for hit in pats.get("hits", []):
        tel.RISK_SIGNALS.add(1, {"kind": hit["kind"], "strength": "strong", "method": "pattern"})
    if pats.get("spam_reason"):
        tel.SPAM.add(1, {"reason": pats["spam_reason"]})
    for ev in (result.get("injection") or {}).get("evidence", []):
        layer = ev.split(":")[0]  # guard / head (patterns are counted from pattern_check below)
        if layer in ("guard", "head"):
            strength = "strong" if "(strong)" in ev else "weak"
            tel.RISK_SIGNALS.add(
                1, {"kind": "prompt_injection", "strength": strength, "method": layer}
            )
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


def _stream(record: TicketRecord, deps: Deps | None, app, config: dict, resume: bool = False):
    """Yield (node, update) as each node finishes, so GET /tickets/{n} shows live progress.
    resume=True: input None = continue from the last checkpoint of this ticket's thread."""
    inp = None if resume else {"ticket_id": record.external_id, **record.masked}
    for chunk in app.stream(inp, config, stream_mode="updates", context=deps):
        yield from chunk.items()


def _failed_result(app, config: dict, err: Exception) -> dict:
    """A ticket that failed every attempt still reaches a person (reason technical_failure),
    with whatever the steps that DID finish found (spam, injection, risks), so the agent and the
    routing see them. The checkpoint holds that partial state."""
    try:
        state = app.get_state(config).values or {}
    except Exception:  # the checkpoint store itself may be what failed
        state = {}
    routing = routing_rules.decide({**state, "reasons": ["technical_failure"]})
    routing.evidence.append(f"error:{type(err).__name__}")
    steps = list(state.get("steps", []))
    return {
        "classification": None,
        "reply": None,
        "citations": None,
        "routing": routing.model_dump(),
        "patterns": state.get("patterns"),
        "injection": state.get("injection"),
        "risk": state.get("risk"),
        "complete": False,
        "steps_completed": sum(NODE_IMPLEMENTED.get(n, True) for n in steps),
        "placeholder_nodes": [n for n in steps if not NODE_IMPLEMENTED.get(n, True)],
        "failed": True,
    }


def _hand_over_failed(record: TicketRecord, result: dict, err: Exception, audit) -> None:
    routing = result["routing"]
    tel.TICKETS.add(
        1,
        {
            "decision": routing["decision"],
            "destination": routing["destination"],
            "complete": "false",
        },
    )
    for reason in routing["reasons"]:
        tel.ROUTING_REASONS.add(1, {"reason": reason})
    log.warning(
        "ticket handed to a person after failing",
        extra={"ticket_number": record.number, "ticket_id": record.external_id,
               "attempts": record.attempts, "reasons": routing["reasons"],
               "queue": routing["destination"]},
    )  # fmt: skip
    if audit is not None:
        audit.write(
            "ticket_failed",
            ticket_number=record.number,
            ticket_id=record.external_id,
            attempts=record.attempts,
            error=type(err).__name__,
            patterns=result["patterns"],
            injection=result["injection"],
            routing=routing,
        )


def _drop_checkpoint(app, config: dict, ids: dict) -> None:
    """Done: the checkpoint is no longer needed (the result is in the tickets table)."""
    try:
        app.checkpointer.delete_thread(config["configurable"]["thread_id"])
    except Exception:  # never fail a finished ticket over cleanup
        log.warning("checkpoint not deleted", extra=ids)


def _record_pii_metrics(report) -> None:
    for d in report.detections:
        tel.PII_DETECTIONS.add(1, {"type": d.type, "recognizer": d.recognizer})
    for kind in report.residual:
        tel.PII_RESIDUAL.add(1, {"kind": kind})
    tel.PII_TICKETS.add(1, {"residual": "yes" if report.residual else "no"})
    tel.PII_MS.record(report.ms)

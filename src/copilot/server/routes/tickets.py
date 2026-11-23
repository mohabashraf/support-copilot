"""Ticket API: the only business endpoints (JSON). Two clients:

  helpdesk (system)
    POST /tickets                          send a ticket → 202 + ticket number (worker runs it)
  agents (through a frontend or the helpdesk)
    GET  /tickets?queue=&status=           inbox: tickets with status, progress, category,
                                           urgency, the co-pilot's decision, reasons, queue
    GET  /tickets/{number}                 one ticket: status, steps, classification, draft,
                                           citations, routing, the agent's decision
    GET  /tickets/{number}/customer-details  real values for the agent (cards, documents and
                                           accounts stay **** 1234); every call is audited
    POST /tickets/{number}/decision        send the draft (as is or edited) or handle manually;
                                           AI draft AND final text are audited

Auth (server/auth.py): `Authorization: Bearer <API key>`; the key decides the role (helpdesk |
agent) and, for agents, WHO it is, so the audit log records the real person. Phase 2: SSO (OIDC).

PII: responses carry masked text only (placeholders like [NAME_1]), except customer-details.
Placeholders in a sent reply are filled in at send time; the final text is stored re-masked.
"""

import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from copilot.audit import AuditLog, text_hash
from copilot.log import get_logger
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import PiiVault, display_value, pii_type
from copilot.pipeline.schemas import Ticket
from copilot.server.auth import Principal, require
from copilot.server.deps import get_audit, get_masker, get_tickets, get_vault
from copilot.tickets.service import intake
from copilot.tickets.store import MAX_ATTEMPTS, Decision, Status, TicketRecord, TicketStore

router = APIRouter(tags=["tickets"])
log = get_logger(__name__)

Queue = Literal["support_agent", "senior_support", "compliance"]
_PLACEHOLDER = re.compile(r"\[[A-Z]+_\d+\]")


# ---- Response models ------------------------------------------------------------------------


class TicketAccepted(BaseModel):
    ticket_number: int
    ticket_id: str
    status: Status
    status_url: str


class AgentAction(BaseModel):
    decision: Decision  # sent | edited | manual
    agent: str
    decided_at: datetime
    final_text_masked: str  # what was sent, re-masked (empty when handled manually)


class StepOut(BaseModel):
    node: str
    status: Literal["completed", "placeholder"]  # placeholder = not implemented, did NOTHING
    ms: float | None


class TicketSummary(BaseModel):
    """One inbox row."""

    ticket_number: int
    ticket_id: str
    status: Status
    steps_completed: str  # "3/8": workflow steps that did REAL work (placeholders don't count)
    complete: bool | None  # True only if every step did real work; None while running
    current_step: str | None
    category: str | None
    urgency: str | None
    decision: str | None  # auto_draft_ok | human_required | null = not decided
    queue: str | None  # support_agent | senior_support | compliance | null = not decided
    reasons: list[str]
    pii_types: list[str]
    handled_by_agent: bool
    created_at: datetime


class Inbox(BaseModel):
    counts: dict[str, int]  # tickets by status
    items: list[TicketSummary]


class TicketStatus(BaseModel):
    ticket_number: int
    ticket_id: str
    status: Status  # queued → processing → done | failed (queued again while retrying)
    current_step: str | None
    steps_completed: str  # "3/8": workflow steps that did REAL work (placeholders don't count)
    complete: bool | None  # True only if every step did real work; None while running
    steps: list[StepOut]  # in order, each with its own status: completed | placeholder
    attempts: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    seconds_in_queue: float | None
    seconds_processing: float | None
    trace_id: str | None  # LangSmith run of the processing (agent support-copilot / local)
    message: dict[str, str]  # the customer's subject/body, MASKED ([NAME_1], [CARD_1], ...)
    pii_types: list[str]
    result: (
        dict[str, Any] | None
    )  # classification, reply, citations, routing (null = not produced)
    agent_action: AgentAction | None
    error: str | None  # last error (when retrying or failed)


class DecisionIn(BaseModel):
    action: Literal["send", "manual"]
    final_text: str = Field(
        "",
        description="The reply to send; may keep placeholders like "
        "[NAME_1], filled in at send time. Ignored for 'manual'.",
    )


# ---- Helpdesk -------------------------------------------------------------------------------


@router.post("/tickets", status_code=status.HTTP_202_ACCEPTED)
def create_ticket(
    ticket: Ticket,
    response: Response,
    _caller: Principal = Depends(require("helpdesk")),
    masker: Masker = Depends(get_masker),
    vault: PiiVault = Depends(get_vault),
    tickets: TicketStore = Depends(get_tickets),
) -> TicketAccepted:
    record, created = intake(ticket, masker, vault, tickets)
    if not created:
        response.status_code = status.HTTP_200_OK  # already known: nothing new was queued
    response.headers["Location"] = f"/tickets/{record.number}"
    return TicketAccepted(
        ticket_number=record.number,
        ticket_id=record.external_id,
        status=record.status,
        status_url=f"/tickets/{record.number}",
    )


# ---- Agents ---------------------------------------------------------------------------------


@router.get("/tickets")
def inbox(
    queue: Queue | None = None,
    status: Status | None = None,
    limit: int = Query(50, ge=1, le=500),
    tickets: TicketStore = Depends(get_tickets),
    _agent: Principal = Depends(require("agent")),
) -> Inbox:
    rows = tickets.list(status=status, destination=queue, limit=limit)
    return Inbox(counts=tickets.counts(), items=[_summary(r) for r in rows])


@router.get("/tickets/{number}")
def get_ticket(
    number: int,
    tickets: TicketStore = Depends(get_tickets),
    _caller: Principal = Depends(require("helpdesk", "agent")),
) -> TicketStatus:
    return _status(_get(tickets, number))


@router.get("/tickets/{number}/customer-details")
def customer_details(
    number: int,
    tickets: TicketStore = Depends(get_tickets),
    vault: PiiVault = Depends(get_vault),
    audit: AuditLog = Depends(get_audit),
    caller: Principal = Depends(require("agent")),
) -> dict[str, str]:
    """Placeholder → value for this ticket. Cards, documents and accounts: last 4 only."""
    agent = caller.name
    r = _get(tickets, number)
    found = sorted(set(_PLACEHOLDER.findall(" ".join(r.masked.values()))))
    values = {}
    for ph in found:
        v = vault.reveal(r.external_id, ph) if vault else None
        values[ph] = display_value(ph, v) if v else "(expired)"
    if audit:
        audit.write(
            "pii_revealed",
            ticket_number=number,
            ticket_id=r.external_id,
            agent=agent,
            types=sorted({pii_type(p) for p in found}),
        )
    log.info("pii revealed", extra={"ticket_number": number, "agent": agent, "count": len(found)})
    return values


@router.post("/tickets/{number}/decision")
def decide(
    number: int,
    body: DecisionIn,
    tickets: TicketStore = Depends(get_tickets),
    vault: PiiVault = Depends(get_vault),
    masker: Masker = Depends(get_masker),
    audit: AuditLog = Depends(get_audit),
    caller: Principal = Depends(require("agent")),
) -> TicketStatus:
    agent = caller.name
    r = _get(tickets, number)
    if r.status != "done":
        raise HTTPException(409, f"ticket {number} is {r.status}: wait until the co-pilot is done")
    if r.agent_decision:
        raise HTTPException(409, f"ticket {number} was already handled by {r.agent_name}")
    draft = (r.result or {}).get("reply") or ""
    if body.action == "manual":
        decision, sent_masked = "manual", ""
    else:
        if not body.final_text.strip():
            raise HTTPException(422, "final_text is empty: send a reply or use action=manual")
        decision = "sent" if body.final_text.strip() == draft.strip() else "edited"
        # Fill placeholders for the customer (Phase 2: posted to the helpdesk), then re-mask for
        # storage, so neither the ticket table nor the audit log holds PII.
        sent = _PLACEHOLDER.sub(lambda m: _fill(vault, r.external_id, m.group()), body.final_text)
        sent_masked = masker.mask(sent).text
    tickets.decide(number, decision, agent, sent_masked)
    if audit:
        audit.write(
            "agent_decision",
            ticket_number=number,
            ticket_id=r.external_id,
            agent=agent,
            decision=decision,
            ai_draft=draft,
            final_text_masked=sent_masked,
            final_text_hash=text_hash(sent_masked),
            routing=(r.result or {}).get("routing"),
        )
    log.info(
        "agent decision", extra={"ticket_number": number, "agent": agent, "decision": decision}
    )
    return _status(_get(tickets, number))


# ---- Helpers --------------------------------------------------------------------------------


def _get(tickets: TicketStore, number: int) -> TicketRecord:
    r = tickets.get(number)
    if r is None:
        raise HTTPException(404, f"ticket {number} not found")
    return r


def _fill(vault: PiiVault | None, ticket_id: str, placeholder: str) -> str:
    value = vault.reveal(ticket_id, placeholder) if vault else None
    return display_value(placeholder, value) if value else placeholder


def _work(r: TicketRecord) -> tuple[str, bool | None, list[StepOut]]:
    """steps_completed, complete, per-step status, from what the worker recorded.
    A placeholder step (not implemented) did nothing, so it never counts as completed.
    Steps saved without the flag (before 2026-11-23) were all placeholders."""
    steps = [
        StepOut(
            node=s["node"],
            status="completed" if s.get("implemented", False) else "placeholder",
            ms=s.get("ms"),
        )
        for s in r.step_times
    ]
    done = sum(s.status == "completed" for s in steps)
    complete = (
        None
        if r.status in ("queued", "processing")
        else (r.status == "done" and done == len(steps) > 0)
    )
    # Denominator = the steps on THIS ticket's path (a spam ticket skips the classifiers).
    return f"{done}/{len(steps)}", complete, steps


def _summary(r: TicketRecord) -> TicketSummary:
    routing = (r.result or {}).get("routing") or {}
    cls = (r.result or {}).get("classification") or {}
    completed, complete, _ = _work(r)
    return TicketSummary(
        ticket_number=r.number,
        ticket_id=r.external_id,
        status=r.status,
        steps_completed=completed,
        complete=complete,
        current_step=r.current_step,
        category=cls.get("category"),
        urgency=cls.get("urgency"),
        decision=routing.get("decision"),
        queue=routing.get("destination"),
        reasons=routing.get("reasons", []),
        pii_types=r.pii_types,
        handled_by_agent=r.agent_decision is not None,
        created_at=r.created_at,
    )


def _status(r: TicketRecord) -> TicketStatus:
    def secs(a: datetime | None, b: datetime | None) -> float | None:
        return round((b - a).total_seconds(), 3) if a and b else None

    completed, complete, steps = _work(r)
    action = None
    if r.agent_decision and r.decided_at:
        action = AgentAction(
            decision=r.agent_decision,
            agent=r.agent_name or "",
            decided_at=r.decided_at,
            final_text_masked=r.final_text_masked or "",
        )
    return TicketStatus(
        ticket_number=r.number,
        ticket_id=r.external_id,
        status=r.status,
        current_step=r.current_step,
        steps_completed=completed,
        complete=complete,
        steps=steps,
        attempts=r.attempts,
        max_attempts=MAX_ATTEMPTS,
        created_at=r.created_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
        seconds_in_queue=secs(r.created_at, r.started_at),
        seconds_processing=secs(r.started_at, r.finished_at),
        trace_id=r.trace_id,
        message=r.masked,
        pii_types=r.pii_types,
        result=r.result if r.status == "done" else None,
        agent_action=action,
        error=r.error,
    )

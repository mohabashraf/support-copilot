"""Ticket store = the job queue (Postgres table `tickets`). No Redis/Celery: Postgres is already
here, durable, and `FOR UPDATE SKIP LOCKED` gives a correct multi-worker queue at this volume
(~10,000 tickets/month ≈ 1–2 per minute at peak).

Life of a ticket:   queued ──claim──► processing ──► done
                       ▲                  │
                       └── error, retry ──┤ (attempts < MAX_ATTEMPTS)
                                          └──► failed (error kept)

- Only MASKED text is stored (masking runs at intake); real values live in the encrypted vault.
- Idempotent intake: the helpdesk's ticket ID (`external_id`) is unique, so a redelivered
  webhook returns the existing ticket instead of creating a second one (PRD NFR-4d).
- Leases: a claimed ticket has `lease_until`; if a worker dies, the ticket becomes claimable
  again when the lease expires, so nothing is silently dropped (PRD NFR-7).
- Retries RESUME (service.py, LangGraph checkpointer): a claim keeps the steps of earlier
  attempts; only a fresh run clears them (`reset_steps`). Each step records its attempt.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

Status = Literal["queued", "processing", "done", "failed"]
Decision = Literal["sent", "edited", "manual"]  # the agent's call on the co-pilot's draft
MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=5)


class TicketRecord(BaseModel):
    number: int  # our ticket number, returned by POST /tickets
    external_id: str  # the helpdesk's ticket ID
    status: Status
    current_step: str | None = None
    steps: list[str] = []
    attempts: int = 0
    masked: dict[str, str]  # masked subject/body: the only text we keep
    pii_types: list[str] = []
    # Masking report, NO values: [{field, placeholder, type, recognizer, score, length}]
    pii_detections: list[dict] = []
    pii_residual: list[str] = []  # possible misses left after masking (long_number, ...)
    pii_ms: float | None = None
    step_times: list[dict] = []  # [{"node", "ms", "implemented"}, ...] in order
    result: dict[str, Any] | None = None  # classification, reply, citations, routing
    error: str | None = None
    intake_trace_id: str | None = None  # LangSmith trace of the intake (masking)
    # OpenTelemetry context of the intake span (W3C traceparent): the worker continues this
    # trace, so ONE trace shows POST → masking → queue wait → every graph step.
    trace_context: str | None = None
    received_at: datetime | None = None  # the API got the request (before masking)
    trace_id: str | None = None  # LangSmith trace of the processing run
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    lease_until: datetime | None = None
    # The agent's decision (PRD §6b: AI draft AND final text, who and when)
    agent_decision: Decision | None = None
    agent_name: str | None = None
    final_text_masked: str | None = None  # what was sent, re-masked: no PII stored here
    decided_at: datetime | None = None


class TicketStore(Protocol):
    def create(
        self,
        external_id: str,
        masked: dict,
        pii_types: list[str],
        intake_trace_id: str,
        pii_detections: list[dict] | None = None,
        pii_residual: list[str] | None = None,
        pii_ms: float | None = None,
        trace_context: str | None = None,
        received_at: datetime | None = None,
    ) -> tuple[TicketRecord, bool]: ...
    def get(self, number: int) -> TicketRecord | None: ...
    def claim(self) -> TicketRecord | None: ...
    def reset_steps(self, number: int) -> None: ...  # a fresh run (not a resume)
    def set_step(
        self,
        number: int,
        step: str,
        trace_id: str | None = None,
        ms: float | None = None,
        implemented: bool = True,
        attempt: int | None = None,
    ) -> None: ...
    def finish(self, number: int, result: dict) -> None: ...
    def set_result(self, number: int, result: dict) -> None: ...  # keeps the status
    def fail(self, number: int, error: str) -> Status: ...
    def counts(self) -> dict[str, int]: ...
    def list(
        self, status: str | None = None, destination: str | None = None, limit: int = 100
    ) -> list[TicketRecord]: ...  # noqa: E501
    def decide(
        self, number: int, decision: Decision, agent: str, final_text_masked: str
    ) -> None: ...  # noqa: E501


_COLUMNS = (
    "number, external_id, status, current_step, steps, attempts, masked, pii_types, result, "
    "error, intake_trace_id, trace_id, created_at, started_at, finished_at, lease_until, "
    "agent_decision, agent_name, final_text_masked, decided_at, "
    "pii_detections, pii_residual, pii_ms, step_times, trace_context, received_at"
)


class PgTicketStore:
    def __init__(self, pool: ConnectionPool, table: str = "tickets"):
        self.pool, self.t = pool, table
        with pool.connection() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                    number          bigserial   PRIMARY KEY,
                    external_id     text        NOT NULL UNIQUE,
                    status          text        NOT NULL DEFAULT 'queued',
                    current_step    text,
                    steps           jsonb       NOT NULL DEFAULT '[]',
                    attempts        int         NOT NULL DEFAULT 0,
                    masked          jsonb       NOT NULL,
                    pii_types       jsonb       NOT NULL DEFAULT '[]',
                    result          jsonb,
                    error           text,
                    intake_trace_id text,
                    trace_id        text,
                    created_at      timestamptz NOT NULL DEFAULT now(),
                    started_at      timestamptz,
                    finished_at     timestamptz,
                    lease_until     timestamptz
                )"""
            )
            # Columns added later (agent decision, masking report): ADD COLUMN IF NOT EXISTS.
            for col in (
                "agent_decision text",
                "agent_name text",
                "final_text_masked text",
                "decided_at timestamptz",
                "pii_detections jsonb NOT NULL DEFAULT '[]'",  # masking report, no values
                "trace_context text",  # W3C traceparent of the intake span
                "received_at timestamptz",  # request received, before masking
                "pii_residual jsonb NOT NULL DEFAULT '[]'",
                "pii_ms double precision",
                "step_times jsonb NOT NULL DEFAULT '[]'",  # [{node, ms}] per finished node
            ):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}")
            # steps_visited counts placeholders too, so it is never shown as progress.
            # Monitoring view for Grafana: derived, NON-TEXT columns only (no message, draft, final
            # text or error text). The read-only role `grafana_ro` may read this view and nothing
            # else (observability/postgres/grafana_readonly.sql), so Grafana can't show PII.
            # Views are derived: drop and recreate so their columns can change (OR REPLACE only
            # allows appending). The GRANT below is re-applied every start.
            conn.execute(
                f"DROP VIEW IF EXISTS {table}_monitoring, {table}_pii_detections, {table}_steps, "
                f"{table}_risk_signals"
            )
            conn.execute(
                f"""CREATE VIEW {table}_monitoring AS SELECT
                    number, external_id AS ticket_id, status, current_step,
                    jsonb_array_length(steps) AS steps_visited,
                    (SELECT count(*) FROM jsonb_array_elements(step_times) e
                     WHERE COALESCE((e->>'implemented')::bool, false)) AS steps_completed,
                    COALESCE((result->>'complete')::bool, false) AS complete, attempts,
                    pii_types::text AS pii_types,
                    result->'classification'->>'category' AS category,
                    result->'classification'->>'urgency' AS urgency,
                    result->'routing'->>'decision' AS decision,
                    result->'routing'->>'destination' AS queue,
                    (result->'routing'->'reasons')::text AS reasons,
                    result->'routing'->>'priority' AS priority,
                    (result->'injection'->>'detected')::bool AS injection_detected,
                    (result->'injection'->'model'->>'probability')::float AS injection_score,
                    (result->'routing'->'evidence')::text AS evidence,
                    agent_decision, agent_name, error IS NOT NULL AS has_error,
                    jsonb_array_length(pii_detections) AS pii_count,
                    pii_residual::text AS pii_residual, round(pii_ms::numeric, 1) AS pii_ms,
                    COALESCE(received_at, created_at) AS received_at,
                    extract(epoch FROM created_at - COALESCE(received_at, created_at))
                        AS intake_seconds,
                    extract(epoch FROM finished_at - COALESCE(received_at, created_at))
                        AS total_seconds,
                    created_at, started_at, finished_at, decided_at,
                    extract(epoch FROM started_at - created_at) AS queue_seconds,
                    extract(epoch FROM finished_at - started_at) AS processing_seconds
                FROM {table}"""
            )
            # One row per masked span (type, recognizer, score, length; never the value).
            conn.execute(
                f"""CREATE VIEW {table}_pii_detections AS SELECT
                    t.number, t.external_id AS ticket_id, d->>'field' AS field,
                    d->>'placeholder' AS placeholder, d->>'type' AS type,
                    d->>'recognizer' AS recognizer, (d->>'score')::float AS score,
                    (d->>'length')::int AS chars
                FROM {table} t, jsonb_array_elements(t.pii_detections) AS d"""
            )
            # One row per workflow node the ticket went through, in order, with its duration.
            # Steps saved without the flag (before 2026-11-23) were all placeholders → false.
            conn.execute(
                f"""CREATE VIEW {table}_steps AS SELECT
                    t.number, s.ord::int AS ord, s.e->>'node' AS node, (s.e->>'ms')::float AS ms,
                    COALESCE((s.e->>'implemented')::bool, false) AS implemented,
                    (s.e->>'attempt')::int AS attempt
                FROM {table} t, jsonb_array_elements(t.step_times) WITH ORDINALITY AS s(e, ord)"""
            )
            # One row per risk signal: exact patterns (pattern name) and classifier signals
            # (probability, strong/weak, nearest TRAINING example). Never the customer's text.
            conn.execute(
                f"""CREATE VIEW {table}_risk_signals AS
                SELECT t.number, h->>'kind' AS kind, 'pattern' AS method, 'strong' AS strength,
                       1.0::float AS probability, h->>'pattern' AS evidence
                FROM {table} t,
                     jsonb_array_elements(COALESCE(t.result->'patterns'->'hits', '[]')) h
                UNION ALL
                SELECT t.number, s->>'kind', s->>'method', s->>'strength',
                       (s->>'probability')::float, s->>'nearest_example'
                FROM {table} t,
                     jsonb_array_elements(COALESCE(t.result->'risk'->'signals', '[]')) s
                UNION ALL
                SELECT t.number, 'prompt_injection', 'model',
                       t.result->'injection'->'model'->>'strength',
                       (t.result->'injection'->'model'->>'probability')::float, 'injection model'
                FROM {table} t
                WHERE t.result->'injection'->'model'->>'strength' IS NOT NULL
                UNION ALL
                SELECT t.number, 'spam_or_empty', 'pattern', 'strong', 1.0,
                       t.result->'patterns'->>'spam_reason'
                FROM {table} t WHERE t.result->'patterns'->>'spam_reason' IS NOT NULL"""
            )
            conn.execute(
                f"""DO $$ BEGIN
                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
                        GRANT SELECT ON {table}_monitoring, {table}_pii_detections,
                            {table}_steps, {table}_risk_signals TO grafana_ro;
                    END IF; END $$"""
            )
            # The worker's claim query scans only claimable rows.
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {table}_claimable ON {table} (number) "
                "WHERE status IN ('queued', 'processing')"
            )

    def _one(self, sql: str, args: tuple) -> TicketRecord | None:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(sql, args).fetchone()
        return TicketRecord(**row) if row else None

    def create(
        self,
        external_id,
        masked,
        pii_types,
        intake_trace_id,
        pii_detections=None,
        pii_residual=None,
        pii_ms=None,
        trace_context=None,
        received_at=None,
    ):
        rec = self._one(
            f"""INSERT INTO {self.t} (external_id, masked, pii_types, intake_trace_id,
                    pii_detections, pii_residual, pii_ms, trace_context, received_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (external_id) DO NOTHING
                RETURNING {_COLUMNS}""",
            (
                external_id,
                Jsonb(masked),
                Jsonb(pii_types),
                intake_trace_id,
                Jsonb(pii_detections or []),
                Jsonb(pii_residual or []),
                pii_ms,
                trace_context,
                received_at,
            ),
        )
        if rec:
            return rec, True
        existing = self._one(
            f"SELECT {_COLUMNS} FROM {self.t} WHERE external_id = %s", (external_id,)
        )
        return existing, False

    def get(self, number):
        return self._one(f"SELECT {_COLUMNS} FROM {self.t} WHERE number = %s", (number,))

    def claim(self):
        """Take the oldest claimable ticket. SKIP LOCKED: concurrent workers never get the same
        row and never wait on each other. Expired leases (crashed worker) are claimable again."""
        return self._one(
            f"""UPDATE {self.t} SET status = 'processing', attempts = attempts + 1,
                    started_at = now(), lease_until = now() + %s, error = NULL,
                    current_step = NULL
                WHERE number = (
                    SELECT number FROM {self.t}
                    WHERE status = 'queued' OR (status = 'processing' AND lease_until < now())
                    ORDER BY number
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
                RETURNING {_COLUMNS}""",
            (LEASE,),
        )

    def reset_steps(self, number):
        with self.pool.connection() as conn:
            conn.execute(
                f"UPDATE {self.t} SET steps = '[]', step_times = '[]' WHERE number = %s", (number,)
            )

    def set_step(self, number, step, trace_id=None, ms=None, implemented=True, attempt=None):
        entry = {"node": step, "ms": ms, "implemented": implemented, "attempt": attempt}
        with self.pool.connection() as conn:
            conn.execute(
                f"""UPDATE {self.t} SET current_step = %s, steps = steps || %s,
                        step_times = step_times || %s,
                        lease_until = now() + %s, trace_id = COALESCE(%s, trace_id)
                    WHERE number = %s""",
                (step, Jsonb([step]), Jsonb([entry]), LEASE, trace_id, number),
            )

    def finish(self, number, result):
        with self.pool.connection() as conn:
            conn.execute(
                f"""UPDATE {self.t} SET status = 'done', result = %s, current_step = NULL,
                        finished_at = now(), lease_until = NULL WHERE number = %s""",
                (Jsonb(result), number),
            )

    def set_result(self, number, result):
        with self.pool.connection() as conn:
            conn.execute(
                f"UPDATE {self.t} SET result = %s WHERE number = %s", (Jsonb(result), number)
            )

    def fail(self, number, error):
        rec = self._one(
            f"""UPDATE {self.t} SET
                    status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'queued' END,
                    error = %s, lease_until = NULL,
                    finished_at = CASE WHEN attempts >= %s THEN now() END
                WHERE number = %s RETURNING {_COLUMNS}""",
            (MAX_ATTEMPTS, error[:2000], MAX_ATTEMPTS, number),
        )
        return rec.status if rec else "failed"

    def counts(self):
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT status, count(*) FROM {self.t} GROUP BY status"
            ).fetchall()
        return {s: n for s, n in rows}

    def list(self, status=None, destination=None, limit=100):
        where, args = [], []
        if status:
            where.append("status = %s")
            args.append(status)
        if destination:
            where.append("result->'routing'->>'destination' = %s")
            args.append(destination)
        sql = f"SELECT {_COLUMNS} FROM {self.t}"
        sql += f" WHERE {' AND '.join(where)}" if where else ""
        sql += " ORDER BY number DESC LIMIT %s"
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(sql, (*args, limit)).fetchall()
        return [TicketRecord(**r) for r in rows]

    def decide(self, number, decision, agent, final_text_masked):
        with self.pool.connection() as conn:
            conn.execute(
                f"""UPDATE {self.t} SET agent_decision = %s, agent_name = %s,
                        final_text_masked = %s, decided_at = now() WHERE number = %s""",
                (decision, agent, final_text_masked, number),
            )


class InMemoryTicketStore:
    """Same behaviour for tests and VECTOR_STORE=memory (single process only)."""

    def __init__(self):
        self.rows: dict[int, TicketRecord] = {}
        self.by_external: dict[str, int] = {}

    def create(
        self,
        external_id,
        masked,
        pii_types,
        intake_trace_id,
        pii_detections=None,
        pii_residual=None,
        pii_ms=None,
        trace_context=None,
        received_at=None,
    ):
        if external_id in self.by_external:
            return self.rows[self.by_external[external_id]], False
        n = len(self.rows) + 1
        rec = TicketRecord(
            number=n,
            external_id=external_id,
            status="queued",
            masked=masked,
            pii_types=pii_types,
            intake_trace_id=intake_trace_id,
            pii_detections=pii_detections or [],
            pii_residual=pii_residual or [],
            pii_ms=pii_ms,
            trace_context=trace_context,
            received_at=received_at,
            created_at=datetime.now(UTC),
        )
        self.rows[n], self.by_external[external_id] = rec, n
        return rec, True

    def get(self, number):
        return self.rows.get(number)

    def claim(self):
        now = datetime.now(UTC)
        for rec in sorted(self.rows.values(), key=lambda r: r.number):
            expired = rec.status == "processing" and rec.lease_until and rec.lease_until < now
            if rec.status == "queued" or expired:
                rec.status, rec.attempts = "processing", rec.attempts + 1
                rec.started_at, rec.lease_until = now, now + LEASE
                rec.error, rec.current_step = None, None
                return rec.model_copy()
        return None

    def reset_steps(self, number):
        self.rows[number].steps, self.rows[number].step_times = [], []

    def set_step(self, number, step, trace_id=None, ms=None, implemented=True, attempt=None):
        rec = self.rows[number]
        rec.current_step, rec.steps = step, [*rec.steps, step]
        entry = {"node": step, "ms": ms, "implemented": implemented, "attempt": attempt}
        rec.step_times = [*rec.step_times, entry]
        rec.lease_until = datetime.now(UTC) + LEASE
        rec.trace_id = trace_id or rec.trace_id

    def finish(self, number, result):
        rec = self.rows[number]
        rec.status, rec.result, rec.current_step = "done", result, None
        rec.finished_at, rec.lease_until = datetime.now(UTC), None

    def set_result(self, number, result):
        self.rows[number].result = result

    def fail(self, number, error):
        rec = self.rows[number]
        rec.error, rec.lease_until = error, None
        rec.status = "failed" if rec.attempts >= MAX_ATTEMPTS else "queued"
        if rec.status == "failed":
            rec.finished_at = datetime.now(UTC)
        return rec.status

    def counts(self):
        out: dict[str, int] = {}
        for rec in self.rows.values():
            out[rec.status] = out.get(rec.status, 0) + 1
        return out

    def list(self, status=None, destination=None, limit=100):
        rows = sorted(self.rows.values(), key=lambda r: -r.number)
        if status:
            rows = [r for r in rows if r.status == status]
        if destination:
            rows = [
                r
                for r in rows
                if ((r.result or {}).get("routing") or {}).get("destination") == destination
            ]
        return [r.model_copy() for r in rows[:limit]]

    def decide(self, number, decision, agent, final_text_masked):
        rec = self.rows[number]
        rec.agent_decision, rec.agent_name = decision, agent
        rec.final_text_masked, rec.decided_at = final_text_masked, datetime.now(UTC)

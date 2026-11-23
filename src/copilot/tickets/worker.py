"""The worker: claims queued tickets from Postgres and runs the workflow (`copilot worker`).

Run one per CPU/LLM-concurrency slot; they coordinate through the table (SKIP LOCKED), so adding
workers needs no other change. Stops cleanly on Ctrl+C / SIGTERM after the current ticket.
"""

import signal
import time

from copilot.audit import AuditLog
from copilot.log import get_logger
from copilot.tickets.service import process
from copilot.tickets.store import TicketStore

log = get_logger(__name__)


def run_once(store: TicketStore, audit: AuditLog | None = None) -> bool:
    """Process one ticket if there is one. Returns False when the queue is empty."""
    record = store.claim()
    if record is None:
        return False
    process(record, store, audit)
    return True


def run_forever(store: TicketStore, poll_s: float = 0.5, audit: AuditLog | None = None) -> None:
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        log.info("worker stopping after the current ticket")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker started", extra={"poll_s": poll_s})
    while not stopping:
        if not run_once(store, audit):
            time.sleep(poll_s)  # Phase 2: LISTEN/NOTIFY instead of polling
    log.info("worker stopped")

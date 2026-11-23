"""Audit log (PRD §6b): the legal record of what the system and the agents did, per ticket.

- Append-only JSON lines (`AUDIT_LOG`, default logs/audit.jsonl). Phase 2: an insert-only
  Postgres table or write-once storage (e.g. S3 Object Lock), retention set by compliance.
- MASKED text only, plus a hash of the masked text: an auditor can see what the AI suggested and
  what the agent sent ("who told the customer this, based on what?") without the log holding PII.
- Not the same as tracing (LangSmith/Grafana): those are for engineers, have limited retention
  and may be sampled; this record is complete and ours.

Events: ticket_processed (worker) · pii_revealed (agent opened customer details) ·
agent_decision (sent / edited / manual, with the AI draft AND the final text).
"""

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()  # one line per event, never interleaved

    def write(self, event: str, **fields) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read(self, ticket_number: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if ticket_number is None or rec.get("ticket_number") == ticket_number:
                out.append(rec)
        return out

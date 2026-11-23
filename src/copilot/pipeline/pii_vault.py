"""PII vault (PRD FR-1.3, §6b): the placeholder → value mapping, encrypted, with an expiry.

    ticket T1: "[CARD_1]" → Fernet("4111 1111 1111 1111")   expires after PII_RETENTION_DAYS

Why it exists: masked text is what the LLM, the graph, LangSmith and the audit log see. The
real values are needed only by our own service (e.g. to show the agent a masked card "**** 1111"
or to look up an account in Phase 2), so they live here, apart from everything else:
- **Encrypted at rest** with Fernet (AES-128-CBC + HMAC, `cryptography` library). The key is
  PII_ENCRYPTION_KEY in .env (Phase 2: a KMS / secrets manager, with key rotation).
- **Shorter life** than the audit log: rows expire after PII_RETENTION_DAYS and `purge_expired`
  deletes them (run daily). The audit log keeps only masked text, so it can live longer.
- **Tighter access**: one table, read through `reveal()` only; nothing logs values.
- **Idempotent**: storing the same ticket twice (webhook redelivery, NFR-4d) changes nothing.

Not derived data: unlike the vector tables, never drop this table on a schema change.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from cryptography.fernet import Fernet
from psycopg_pool import ConnectionPool

# Types whose real value is never shown back in full (PRD FR-1.3): at most the last 4 characters.
NEVER_SHOWN_IN_FULL = {"CARD", "DOC", "ACCOUNT"}


def pii_type(placeholder: str) -> str:
    """'[CARD_1]' → 'CARD'"""
    return placeholder.strip("[]").rsplit("_", 1)[0]


def display_value(placeholder: str, value: str) -> str:
    """What a reply or an agent screen may show: '**** 1111' for cards, documents, accounts."""
    if pii_type(placeholder) in NEVER_SHOWN_IN_FULL:
        tail = "".join(c for c in value if c.isalnum())[-4:]
        return f"**** {tail}"
    return value


class PiiVault(Protocol):
    def store(self, ticket_id: str, mapping: dict[str, str]) -> None: ...
    def reveal(self, ticket_id: str, placeholder: str) -> str | None: ...
    def purge_expired(self) -> int: ...


class _Crypto:
    def __init__(self, key: str, retention_days: int):
        self.fernet = Fernet(key.encode())
        self.retention = timedelta(days=retention_days)

    def encrypt(self, value: str) -> bytes:
        return self.fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self.fernet.decrypt(token).decode("utf-8")


class PgPiiVault(_Crypto):
    """Postgres table `pii_vault`. Values are encrypted before they leave this process."""

    def __init__(self, pool: ConnectionPool, key: str, retention_days: int, table="pii_vault"):
        super().__init__(key, retention_days)
        self.pool, self.table = pool, table
        with pool.connection() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                    ticket_id   text        NOT NULL,
                    placeholder text        NOT NULL,
                    pii_type    text        NOT NULL,
                    value_enc   bytea       NOT NULL,
                    created_at  timestamptz NOT NULL DEFAULT now(),
                    expires_at  timestamptz NOT NULL,
                    PRIMARY KEY (ticket_id, placeholder)
                )"""
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_expires ON {table} (expires_at)")

    def store(self, ticket_id: str, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        expires = datetime.now(UTC) + self.retention
        rows = [
            (ticket_id, ph, pii_type(ph), self.encrypt(v), expires) for ph, v in mapping.items()
        ]
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {self.table}
                    (ticket_id, placeholder, pii_type, value_enc, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ticket_id, placeholder) DO NOTHING""",
                rows,
            )

    def reveal(self, ticket_id: str, placeholder: str) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"""SELECT value_enc FROM {self.table}
                    WHERE ticket_id = %s AND placeholder = %s AND expires_at > now()""",
                (ticket_id, placeholder),
            ).fetchone()
        return self.decrypt(bytes(row[0])) if row else None

    def purge_expired(self) -> int:
        with self.pool.connection() as conn:
            cur = conn.execute(f"DELETE FROM {self.table} WHERE expires_at <= now()")
            return cur.rowcount


class InMemoryPiiVault(_Crypto):
    """Same behaviour without Postgres (VECTOR_STORE=memory, unit tests). Still encrypted."""

    def __init__(self, key: str, retention_days: int):
        super().__init__(key, retention_days)
        self.rows: dict[tuple[str, str], tuple[bytes, datetime]] = {}

    def store(self, ticket_id: str, mapping: dict[str, str]) -> None:
        expires = datetime.now(UTC) + self.retention
        for ph, v in mapping.items():
            self.rows.setdefault((ticket_id, ph), (self.encrypt(v), expires))

    def reveal(self, ticket_id: str, placeholder: str) -> str | None:
        row = self.rows.get((ticket_id, placeholder))
        if row is None or row[1] <= datetime.now(UTC):
            return None
        return self.decrypt(row[0])

    def purge_expired(self) -> int:
        now = datetime.now(UTC)
        expired = [k for k, (_, exp) in self.rows.items() if exp <= now]
        for k in expired:
            del self.rows[k]
        return len(expired)

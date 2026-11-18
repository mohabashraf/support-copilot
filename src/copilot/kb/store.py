"""Postgres copy of the knowledge base (D8, D9).

`copilot kb index` writes it. The server reads it on every request, through `KBReader`
(explorer, /health) and through the search JOIN in retrieval/index.py. There is no in-memory
copy while serving, so a KB update is visible to every server process immediately.
Each chunk is stored whole as JSON and validated by the Chunk model when it is read back.
"""

from typing import Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from copilot.kb.models import Chunk, Diff, KnowledgeBase, Version, diff, kb_fingerprint


class KBReader(Protocol):
    """What the explorer and /health read.

    KBStore (Postgres, live) and KnowledgeBase (memory mode, tests) both implement it.
    """

    def all_chunks(self) -> list[Chunk]: ...
    def get_chunk(self, chunk_id: str) -> Chunk | None: ...
    def summary(self) -> tuple[str, int]:
        """(kb_hash, number of chunks)"""
        ...


class KBStore:
    def __init__(self, pool: ConnectionPool, table: str = "kb_chunks"):
        self.pool, self.table = pool, table
        with pool.connection() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                        chunk_id     text PRIMARY KEY,
                        position     int NOT NULL,          -- page order, so the KB reads the same
                        content_hash text NOT NULL,
                        data         jsonb NOT NULL         -- the whole Chunk
                    )"""
            )

    # ---- read (server) ----
    def all_chunks(self) -> list[Chunk]:
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT data FROM {self.table} ORDER BY position").fetchall()
        return [Chunk.model_validate(data) for (data,) in rows]

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT data FROM {self.table} WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()
        return Chunk.model_validate(row[0]) if row else None

    def ordered_versions(self) -> list[Version]:
        """(chunk_id, content_hash) in page order: cheap, no chunk data."""
        with self.pool.connection() as conn:
            return conn.execute(
                f"SELECT chunk_id, content_hash FROM {self.table} ORDER BY position"
            ).fetchall()

    def summary(self) -> tuple[str, int]:
        v = self.ordered_versions()
        return kb_fingerprint(v), len(v)

    def load(self) -> KnowledgeBase | None:
        """The whole KB, or None if it was never indexed."""
        chunks = self.all_chunks()
        return KnowledgeBase.from_chunks(chunks) if chunks else None

    # ---- write (only `copilot kb index`) ----
    def sync(self, kb: KnowledgeBase) -> Diff:
        """Make the table match `kb` and report what changed.

        Rows are cheap (no embedding), so every row is rewritten to keep `position` right;
        removed chunks are deleted. One transaction: a reader never sees half an update.
        """
        d = diff(kb.chunk_hashes, dict(self.ordered_versions()))
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {self.table} (chunk_id, position, content_hash, data)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        position = EXCLUDED.position,
                        content_hash = EXCLUDED.content_hash,
                        data = EXCLUDED.data""",
                [
                    (c.id, pos, c.content_hash, Jsonb(c.model_dump()))
                    for pos, c in enumerate(kb.chunks)
                ],
            )
            if d.removed:
                cur.execute(f"DELETE FROM {self.table} WHERE chunk_id = ANY(%s)", (d.removed,))
        return d

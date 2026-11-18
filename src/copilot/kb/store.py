"""Postgres copy of the knowledge base (D8).

`copilot kb index` writes it; the server reads the KB from here, never from the raw HTML.
Each chunk is stored whole as JSON and validated by the Chunk model when it is read back.
"""

from psycopg.types.json import Jsonb

from copilot.kb.models import Chunk, Diff, KnowledgeBase, diff


class KBStore:
    def __init__(self, database_url: str, table: str = "kb_chunks"):
        import psycopg

        self.table = table
        self.conn = psycopg.connect(database_url, autocommit=True)
        self.conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                    chunk_id     text PRIMARY KEY,
                    position     int NOT NULL,          -- page order, so the KB reads the same
                    content_hash text NOT NULL,
                    data         jsonb NOT NULL         -- the whole Chunk
                )"""
        )

    def hashes(self) -> dict[str, str]:
        return dict(self.conn.execute(f"SELECT chunk_id, content_hash FROM {self.table}"))

    def sync(self, kb: KnowledgeBase) -> Diff:
        """Make the table match `kb` and report what changed.

        Rows are cheap (no embedding), so every row is rewritten to keep `position` right;
        removed chunks are deleted. One transaction: a reader never sees half an update.
        """
        d = diff(kb.chunk_hashes, self.hashes())
        with self.conn.transaction(), self.conn.cursor() as cur:
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

    def load(self) -> KnowledgeBase | None:
        """The KB as last written by `copilot kb index`, or None if it was never indexed."""
        rows = self.conn.execute(f"SELECT data FROM {self.table} ORDER BY position").fetchall()
        if not rows:
            return None
        return KnowledgeBase.from_chunks([Chunk.model_validate(data) for (data,) in rows])

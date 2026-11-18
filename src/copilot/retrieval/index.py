"""Vector index (D7, D9): one interface, two implementations.

- InMemoryIndex: numpy, for unit tests and the no-Docker quick start.
- PgVectorIndex: Postgres + pgvector, for the app, eval runs and Phase 2.

Versioned (D9): rows are keyed by (chunk_id, content_hash). Ingestion adds the new version of a
changed chunk next to the old one, then deletes versions nothing uses. Search returns each hit
WITH its chunk: in Postgres a JOIN on the exact version, so the text and the vector always come
from the same chunk version, even while `copilot kb index` is running.
"""

import re
from typing import Protocol

import numpy as np
from psycopg_pool import ConnectionPool

from copilot.kb.models import Chunk, Version


class VectorIndex(Protocol):
    def versions(self) -> set[Version]:
        """Every (chunk_id, content_hash) that has a vector."""
        ...

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        """Store one vector per chunk version. Existing versions are left alone."""
        ...

    def delete(self, versions: set[Version]) -> None: ...

    def search(
        self, query: np.ndarray, k: int, exclude_flags: tuple[str, ...] = ()
    ) -> list[tuple[Chunk, float]]:
        """Up to k (chunk, cosine similarity), best first. Only versions that are served."""
        ...


class InMemoryIndex:
    """Holds the chunks with their vectors, so search returns both from one place."""

    def __init__(self) -> None:
        self._rows: dict[Version, tuple[Chunk, np.ndarray]] = {}

    def versions(self) -> set[Version]:
        return set(self._rows)

    def upsert(self, chunks, vectors) -> None:
        for c, v in zip(chunks, vectors, strict=True):
            self._rows[(c.id, c.content_hash)] = (c, np.asarray(v, dtype=np.float32))

    def delete(self, versions) -> None:
        for key in versions:
            self._rows.pop(key, None)

    def search(self, query, k, exclude_flags=()):
        rows = [(c, v) for c, v in self._rows.values() if not set(c.flags) & set(exclude_flags)]
        if not rows:
            return []
        scores = np.stack([v for _, v in rows]) @ query  # normalised: dot product = cosine
        order = np.argsort(-scores)[:k]
        return [(rows[i][0], float(scores[i])) for i in order]


class PgVectorIndex:
    """One vector table per embedding model (each model has its own vector length), searched
    together with the chunks table (kb/store.py). Uses the shared pool (copilot/db.py)."""

    def __init__(
        self, pool: ConnectionPool, model_name: str, dim: int, chunks_table: str = "kb_chunks"
    ):
        self.pool, self.dim, self.chunks_table = pool, dim, chunks_table
        self.table = "chunk_vectors_" + re.sub(r"[^a-z0-9]", "_", model_name.lower())
        with pool.connection() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.table} (
                        chunk_id     text NOT NULL,
                        content_hash text NOT NULL,
                        flags        text[] NOT NULL DEFAULT '{{}}',
                        embedding    vector({dim}) NOT NULL,
                        PRIMARY KEY (chunk_id, content_hash)   -- one row per chunk VERSION
                    )"""
            )
            # HNSW index: fast approximate search with cosine distance (useful as the KB grows).
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self.table}_hnsw ON {self.table} "
                "USING hnsw (embedding vector_cosine_ops)"
            )

    def versions(self) -> set[Version]:
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT chunk_id, content_hash FROM {self.table}").fetchall()
        return {(cid, h) for cid, h in rows}

    def upsert(self, chunks, vectors) -> None:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {self.table} (chunk_id, content_hash, flags, embedding)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (chunk_id, content_hash) DO NOTHING""",
                [
                    (c.id, c.content_hash, list(c.flags), np.asarray(v, dtype=np.float32))
                    for c, v in zip(chunks, vectors, strict=True)
                ],
            )

    def delete(self, versions) -> None:
        if not versions:
            return
        ids, hashes = zip(*versions, strict=True)
        with self.pool.connection() as conn:
            conn.execute(
                f"""DELETE FROM {self.table}
                    WHERE (chunk_id, content_hash) IN
                          (SELECT * FROM unnest(%s::text[], %s::text[]))""",
                (list(ids), list(hashes)),
            )

    def search(self, query, k, exclude_flags=()):
        # <=> is pgvector's cosine DISTANCE (0 = identical); similarity = 1 - distance.
        # The JOIN keeps only vectors whose exact version is the one in kb_chunks right now.
        q = np.asarray(query, dtype=np.float32)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT c.data, 1 - (v.embedding <=> %s) AS score
                    FROM {self.table} v
                    JOIN {self.chunks_table} c
                      ON c.chunk_id = v.chunk_id AND c.content_hash = v.content_hash
                    WHERE NOT (v.flags && %s::text[])
                    ORDER BY v.embedding <=> %s
                    LIMIT %s""",
                (q, list(exclude_flags), q, k),
            ).fetchall()
        return [(Chunk.model_validate(data), float(score)) for data, score in rows]
